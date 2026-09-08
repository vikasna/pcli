import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from pcli.agent.activity import ActivityTracker
from pcli.agent.loop import AgentLoop, ToolResultEvent, TurnCompleteEvent
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.llm.models import ChatMessage
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.permissions.policy import PermissionPolicy
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.subagent_tool import SPAWN_SUBAGENT, SPAWN_SUBAGENT_TOOL_NAME
from pcli.tools.registry import ToolRegistry


def _settings(**overrides) -> Settings:
    defaults = {
        "gateway_base_url": "http://fake-gateway.test/v1",
        "gateway_api_key": "test-key",
        "default_model": "fake-model",
        "max_retries": 1,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str, *, usage: dict | None = None) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
    if usage:
        chunks[0]["usage"] = usage
    return httpx.Response(200, content=_sse(*chunks))


class _FakeSandbox(Sandbox):
    name = "fake"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("no real tool execution needed for these tests")


async def _noop_echo_handler(arguments: dict, ctx: ToolContext):
    from pcli.tools.base import ToolResult

    return ToolResult(output="echoed")


def _make_registry_with_echo_and_subagent() -> ToolRegistry:
    from pcli.tools.base import ToolSpec

    echo = ToolSpec(
        name="echo_tool",
        description="Echo.",
        parameters={"type": "object", "properties": {}},
        handler=_noop_echo_handler,
        needs_permission=False,
    )
    registry = ToolRegistry()
    registry.register(echo)
    registry.register(SPAWN_SUBAGENT)
    return registry


def _make_ctx(tmp_path: Path, registry: ToolRegistry, client: GatewayClient, permission_manager: PermissionManager) -> ToolContext:
    return ToolContext(
        sandbox=_FakeSandbox(),
        guardrails=permission_manager.guardrails,
        cwd=tmp_path,
        gateway_client=client,
        model="fake-model",
        tool_registry=registry,
        permission_manager=permission_manager,
        ask=None,
        max_tool_iterations=25,
    )


@pytest.mark.asyncio
async def test_spawn_subagent_missing_context_reports_error(tmp_path: Path):
    ctx = ToolContext(sandbox=_FakeSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)
    assert result.is_error is True
    assert "aren't available" in result.output
    assert "[pcli] Suggestion:" in result.output
    assert "directly" in result.output


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_returns_final_text_and_usage(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response(
            "The answer is 42.",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        result = await SPAWN_SUBAGENT.handler({"task": "What is the answer?"}, ctx)

    assert result.is_error is False
    assert "The answer is 42." in result.output
    assert len(result.extra_usage) == 1
    assert result.extra_usage[0].total_tokens == 15


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_gateway_error_includes_actionable_hint(tmp_path: Path):
    """subagent_tool.py's except Exception already catches GatewayError and
    reports f"Subagent failed: {exc}" - since the hint (e.g. which config
    setting to raise) is folded directly into GatewayError.message rather
    than a separate attribute, this call site picks it up automatically,
    with no subagent_tool.py change needed."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("the read operation timed out")
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        result = await SPAWN_SUBAGENT.handler({"task": "What is the answer?"}, ctx)

    assert result.is_error is True
    assert "Subagent failed" in result.output
    assert "request_timeout_s" in result.output
    assert "[pcli] Suggestion:" in result.output  # subagent_tool.py's own generic suggestion


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_registry_never_contains_itself(tmp_path: Path):
    """Prove the depth cap is structural: intercept the nested AgentLoop's
    tool_registry rather than trusting the summary text."""
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.builtin.subagent_tool as subagent_module

    subagent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = _make_registry_with_echo_and_subagent()
        assert SPAWN_SUBAGENT_TOOL_NAME in registry

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)
    finally:
        subagent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert len(captured_registries) == 1
    assert SPAWN_SUBAGENT_TOOL_NAME not in captured_registries[0]
    assert "echo_tool" in captured_registries[0]


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_inherits_max_response_tokens_and_temperature(tmp_path: Path):
    """Regression coverage for a real gap: a fresh AgentLoop defaults both
    to None, so without this a subagent silently ran with no response-
    length cap and the gateway's default temperature regardless of what
    /max-response-tokens or /temperature had configured on the parent."""
    captured_kwargs: list[dict] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_kwargs.append(kwargs)
        return original_init(self, *args, **kwargs)

    import pcli.tools.builtin.subagent_tool as subagent_module

    subagent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = _make_registry_with_echo_and_subagent()

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            ctx = replace(ctx, max_response_tokens=1234, temperature=0.55)
            await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)
    finally:
        subagent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert captured_kwargs[0]["max_response_tokens"] == 1234
    assert captured_kwargs[0]["temperature"] == 0.55


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_respects_allowed_tools_filter(tmp_path: Path):
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.builtin.subagent_tool as subagent_module

    subagent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = _make_registry_with_echo_and_subagent()

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            await SPAWN_SUBAGENT.handler(
                {"task": "do something", "allowed_tools": ["echo_tool"]}, ctx
            )
    finally:
        subagent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert len(captured_registries[0]) == 1
    assert "echo_tool" in captured_registries[0]


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_excludes_non_plan_mode_safe_tools_when_parent_is_in_plan_mode(
    tmp_path: Path,
):
    """A subagent spawned while the parent is in plan mode can't be used as
    a bypass: it inherits the same plan_mode_safe restriction the parent's
    own registry filtering would apply."""
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.builtin.subagent_tool as subagent_module

    subagent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        # echo_tool is plan_mode_safe=False (the default) - only SPAWN_SUBAGENT
        # itself (plan_mode_safe=True) should survive the filter, and it's
        # excluded anyway by the "never spawn further subagents" rule.
        registry = _make_registry_with_echo_and_subagent()

        async with GatewayClient(_settings()) as client:
            ctx = replace(_make_ctx(tmp_path, registry, client, permission_manager), plan_mode=True)
            await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)
    finally:
        subagent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert len(captured_registries) == 1
    assert len(captured_registries[0]) == 0  # echo_tool filtered out, spawn_subagent excluded too


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_can_actually_call_a_tool(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "echo_tool", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        ),
        _text_response("Used the echo tool successfully."),
    ]

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        result = await SPAWN_SUBAGENT.handler({"task": "echo something"}, ctx)

    assert result.is_error is False
    assert "1 tool call(s)" in result.output
    assert "Used the echo tool successfully." in result.output
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_reports_activity_progress(tmp_path: Path):
    """The status pane needs live progress while a subagent runs — verify the
    ActivityTracker sees a start snapshot, a per-tool-call progress snapshot,
    and is cleared again once the subagent finishes."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "echo_tool", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        ),
        _text_response("Used the echo tool successfully."),
    ]

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()
    activity = ActivityTracker()
    snapshots: list[tuple[str, int, str | None] | None] = []

    def _snapshot() -> None:
        sub = activity.subagent
        snapshots.append((sub.task, sub.tool_calls, sub.last_tool) if sub else None)

    activity.subscribe(_snapshot)

    async with GatewayClient(_settings()) as client:
        ctx = replace(_make_ctx(tmp_path, registry, client, permission_manager), activity=activity)
        result = await SPAWN_SUBAGENT.handler({"task": "echo something"}, ctx)

    assert result.is_error is False
    assert snapshots[0] == ("echo something", 0, None)
    assert ("echo something", 1, "echo_tool") in snapshots
    assert snapshots[-1] is None
    assert activity.subagent is None


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_propagates_subagent_cost_via_extra_usage(tmp_path: Path):
    """End-to-end through the *parent* AgentLoop: a top-level turn that calls
    spawn_subagent should surface the subagent's own LLM usage in the
    ToolResultEvent so cost tracking doesn't silently drop it."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        # Parent turn: model requests spawn_subagent.
        httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "spawn_subagent",
                                            "arguments": json.dumps({"task": "look something up"}),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        ),
        # Subagent's own single-turn call.
        _text_response(
            "Found it.", usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        ),
        # Parent's follow-up turn after getting the subagent's result back.
        _text_response("The subagent found it."),
    ]

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = ToolRegistry()
    registry.register(SPAWN_SUBAGENT)

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            model="fake-model",
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_FakeSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                gateway_client=client,
                model="fake-model",
                tool_registry=registry,
                permission_manager=permission_manager,
                ask=None,
            ),
        )
        async def ask(tool_name, arguments, risk_description):
            return ("allow", "once")

        events = []
        async for event in loop.run_turn(
            [ChatMessage(role="user", content="look something up")], ask=ask
        ):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert len(tool_results[0].extra_usage) == 1
    assert tool_results[0].extra_usage[0].total_tokens == 10

    turn_complete = [e for e in events if isinstance(e, TurnCompleteEvent)]
    assert turn_complete[0].new_messages[-1].content == "The subagent found it."
