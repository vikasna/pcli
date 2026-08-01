import json
from pathlib import Path

import httpx
import pytest
import respx

from pcli.agent.loop import AgentLoop, ToolResultEvent, TurnCompleteEvent
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.llm.models import ChatMessage
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.permissions.policy import PermissionPolicy
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.builtin.artifact_tool import FETCH_ARTIFACT
from pcli.tools.registry import ToolRegistry


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        max_retries=1,
    )


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("not used in these tests")


def _big_output_handler_factory(size: int):
    async def _handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(output="x" * size)

    return _handler


def _make_big_tool(size: int, name: str = "big_tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Produces a large result.",
        parameters={"type": "object", "properties": {}},
        handler=_big_output_handler_factory(size),
        needs_permission=False,
    )


def _tool_call_chunks(name: str) -> list[dict]:
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "function": {"name": name, "arguments": "{}"}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _final_text_chunks(text: str = "done") -> list[dict]:
    return [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]


@pytest.mark.asyncio
@respx.mock
async def test_large_tool_output_gets_truncated_and_archived(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_chunks("big_tool"))),
        httpx.Response(200, content=_sse(*_final_text_chunks())),
    ]

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    registry = ToolRegistry()
    registry.register(_make_big_tool(10_000))
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_NullSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                artifact_store=artifacts,
            ),
            artifact_threshold_chars=4000,
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="go")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    result = tool_results[0]
    assert result.artifact_id is not None
    assert len(result.output) < 10_000  # truncated in what's shown/sent to the model
    assert f"artifact_id='{result.artifact_id}'" in result.output
    assert "10000 chars total" in result.output

    # The full original content really is archived and retrievable.
    assert artifacts.get(result.artifact_id) == "x" * 10_000

    # And the truncated (not full) text is what actually went to the model.
    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    tool_message = next(m for m in turn_complete.new_messages if m.role == "tool")
    assert tool_message.content == result.output
    assert len(tool_message.content) < 10_000


@pytest.mark.asyncio
@respx.mock
async def test_small_tool_output_is_not_truncated(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_chunks("big_tool"))),
        httpx.Response(200, content=_sse(*_final_text_chunks())),
    ]

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    registry = ToolRegistry()
    registry.register(_make_big_tool(100))
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_NullSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                artifact_store=artifacts,
            ),
            artifact_threshold_chars=4000,
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="go")]):
            events.append(event)

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.artifact_id is None
    assert result.output == "x" * 100


@pytest.mark.asyncio
@respx.mock
async def test_without_artifact_store_large_output_passes_through_untouched(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_chunks("big_tool"))),
        httpx.Response(200, content=_sse(*_final_text_chunks())),
    ]

    registry = ToolRegistry()
    registry.register(_make_big_tool(10_000))
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_NullSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                artifact_store=None,
            ),
            artifact_threshold_chars=4000,
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="go")]):
            events.append(event)

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.artifact_id is None
    assert result.output == "x" * 10_000  # nowhere to archive it, so it's left as-is


@pytest.mark.asyncio
@respx.mock
async def test_custom_threshold_changes_cutoff(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_chunks("big_tool"))),
        httpx.Response(200, content=_sse(*_final_text_chunks())),
    ]

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    registry = ToolRegistry()
    registry.register(_make_big_tool(500))
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_NullSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                artifact_store=artifacts,
            ),
            artifact_threshold_chars=100,  # much lower than the 500-char output
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="go")]):
            events.append(event)

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.artifact_id is not None
    assert artifacts.get(result.artifact_id) == "x" * 500


@pytest.mark.asyncio
@respx.mock
async def test_full_round_trip_through_fetch_artifact_tool(tmp_path: Path):
    """A tool call produces a huge result -> gets archived -> the model then
    calls fetch_artifact to page through it -> gets the real content back."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    big_content = "".join(f"line-{i}\n" for i in range(1000))  # well over 4000 chars
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_chunks("big_tool"))),
        httpx.Response(200, content=_sse(*_final_text_chunks())),
    ]

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    async def _handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(output=big_content)

    big_tool = ToolSpec(
        name="big_tool",
        description="Produces a large result.",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        needs_permission=False,
    )

    registry = ToolRegistry()
    registry.register(big_tool)
    registry.register(FETCH_ARTIFACT)
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_NullSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                artifact_store=artifacts,
            ),
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="go")]):
            events.append(event)

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    artifact_id = result.artifact_id
    assert artifact_id is not None

    # Now simulate the model calling fetch_artifact directly (as it would in a
    # later turn) and confirm it gets the real content back, paginated.
    ctx = ToolContext(
        sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, artifact_store=artifacts
    )
    fetched = await FETCH_ARTIFACT.handler({"artifact_id": artifact_id, "limit": 100}, ctx)
    assert fetched.output.startswith(big_content[:100])
