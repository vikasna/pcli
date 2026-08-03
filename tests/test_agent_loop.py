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
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
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


class FakeSandbox(Sandbox):
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[ExecRequest] = []

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supports_network_isolation=False, supports_memory_limit=False, supports_cpu_limit=False
        )

    async def execute(self, request: ExecRequest) -> ExecResult:
        self.calls.append(request)
        return ExecResult(stdout="fake output", stderr="", exit_code=0, timed_out=False, backend_used=self.name)


async def _echo_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output=f"echoed: {arguments['text']}")


ECHO_TOOL = ToolSpec(
    name="echo_tool",
    description="Echoes text back.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    handler=_echo_handler,
    needs_permission=False,
)


def _first_two_tool_call_chunks() -> list[dict]:
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "echo_tool", "arguments": '{"text": "hi"}'},
                            }
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
async def test_agent_loop_dispatches_tool_and_continues(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_first_two_tool_call_chunks())),
        httpx.Response(200, content=_sse(*_final_text_chunks("all done"))),
    ]

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    sandbox = FakeSandbox()

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=sandbox, guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )

        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="use the tool")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].output == "echoed: hi"
    assert tool_results[0].is_error is False

    turn_complete = [e for e in events if isinstance(e, TurnCompleteEvent)]
    assert len(turn_complete) == 1
    roles = [m.role for m in turn_complete[0].new_messages]
    assert roles == ["assistant", "tool", "assistant"]
    assert turn_complete[0].new_messages[-1].content == "all done"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_denies_tool_via_guardrail(tmp_path: Path):
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
                                        "function": {
                                            "name": "run_shell",
                                            "arguments": '{"command": "rm -rf /"}',
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
        httpx.Response(200, content=_sse(*_final_text_chunks("understood"))),
    ]

    from pcli.tools.builtin.shell_tool import RUN_SHELL

    registry = ToolRegistry()
    registry.register(RUN_SHELL)
    guardrails = GuardrailsConfig(shell_denylist=["rm -rf /"])
    permission_manager = PermissionManager(
        guardrails=guardrails, policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    sandbox = FakeSandbox()

    async def ask_should_not_be_called(*args, **kwargs):
        raise AssertionError("guardrail should deny before ask() is ever called")

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=sandbox, guardrails=guardrails, cwd=tmp_path
            ),
        )
        events = []
        async for event in loop.run_turn(
            [ChatMessage(role="user", content="delete everything")], ask=ask_should_not_be_called
        ):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "Permission denied" in tool_results[0].output
    assert sandbox.calls == []  # never reached the sandbox


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_asks_for_permission_and_executes_on_allow(tmp_path: Path):
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
                                        "function": {
                                            "name": "run_shell",
                                            "arguments": '{"command": "echo hi"}',
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
        httpx.Response(200, content=_sse(*_final_text_chunks("ok"))),
    ]

    from pcli.tools.builtin.shell_tool import RUN_SHELL

    registry = ToolRegistry()
    registry.register(RUN_SHELL)
    guardrails = GuardrailsConfig()
    permission_manager = PermissionManager(
        guardrails=guardrails, policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    sandbox = FakeSandbox()

    async def ask(tool_name, arguments, risk_description):
        assert tool_name == "run_shell"
        return ("allow", "once")

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=sandbox, guardrails=guardrails, cwd=tmp_path
            ),
        )
        events = []
        async for event in loop.run_turn(
            [ChatMessage(role="user", content="run echo")], ask=ask
        ):
            events.append(event)

    assert len(sandbox.calls) == 1
    assert sandbox.calls[0].command == "echo hi"
    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert tool_results[0].is_error is False
    assert "fake output" in tool_results[0].output


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_invalid_arguments_reported_as_error(tmp_path: Path):
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
        httpx.Response(200, content=_sse(*_final_text_chunks("noted"))),
    ]

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="go")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert tool_results[0].is_error is True
    assert "schema validation" in tool_results[0].output


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_enforces_max_tool_calls_per_turn(tmp_path: Path):
    """A guardrail limit of 1 tool call per turn: the model requests two in
    one batch, only the first should actually execute — the rest of the
    batch still gets a (denied) tool response each, per protocol, and the
    loop stops without a further LLM round-trip."""
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
                                        "function": {"name": "echo_tool", "arguments": '{"text": "a"}'},
                                    },
                                    {
                                        "index": 1,
                                        "id": "call_2",
                                        "function": {"name": "echo_tool", "arguments": '{"text": "b"}'},
                                    },
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        ),
    ]

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)
    guardrails = GuardrailsConfig(max_tool_calls_per_turn=1)
    permission_manager = PermissionManager(
        guardrails=guardrails, policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    sandbox = FakeSandbox()

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=sandbox, guardrails=guardrails, cwd=tmp_path
            ),
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="use the tool twice")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 2
    assert tool_results[0].is_error is False
    assert tool_results[0].output == "echoed: a"
    assert tool_results[1].is_error is True
    assert "guardrail limit" in tool_results[1].output

    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    assert [m.role for m in turn_complete.new_messages] == ["assistant", "tool", "tool", "assistant"]
    assert "Reached the guardrail limit" in turn_complete.new_messages[-1].content
    assert route.call_count == 1  # stopped after this batch, no further chat_stream call


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_truncates_output_over_guardrail_max_output_bytes(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_first_two_tool_call_chunks())),
        httpx.Response(200, content=_sse(*_final_text_chunks("done"))),
    ]

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)
    guardrails = GuardrailsConfig(max_output_bytes=5)
    permission_manager = PermissionManager(
        guardrails=guardrails, policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    sandbox = FakeSandbox()

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=sandbox, guardrails=guardrails, cwd=tmp_path
            ),
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="use the tool")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    # "echoed: hi" is 10 chars; guardrail caps it to 5.
    assert tool_results[0].output.startswith("echoe")
    assert "output truncated to the guardrail limit" in tool_results[0].output
