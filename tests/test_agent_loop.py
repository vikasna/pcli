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


def _single_tool_call_round(call_id: str) -> list[dict]:
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {"name": "echo_tool", "arguments": '{"text": "x"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
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
    # The guardrail's actual reason is included, not just a bare "Permission
    # denied." - see test_dispatch_includes_the_deny_reason_in_the_message
    # below for a more targeted assertion on this.
    assert "rm -rf /" in tool_results[0].output
    assert sandbox.calls == []  # never reached the sandbox


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_includes_the_deny_reason_in_the_message(tmp_path: Path):
    """Regression coverage for a real gap: a bare "Permission denied." gave
    the model nothing to diagnose. AgentLoop now calls
    PermissionManager.check_with_reason() instead of check(), and folds
    whatever reason it returns into the message the model actually sees."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_first_two_tool_call_chunks())),
        httpx.Response(200, content=_sse(*_final_text_chunks("done"))),
    ]

    # ECHO_TOOL's needs_permission=False would bypass ask() entirely (via
    # default_allow) - this test needs a tool that actually reaches it.
    permission_needing_echo = ToolSpec(
        name="echo_tool",
        description="Echoes text back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_echo_handler,
        needs_permission=True,
    )
    registry = ToolRegistry()
    registry.register(permission_needing_echo)
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(max_tool_calls_per_minute=0),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
    )

    async def ask_deny(*args, **kwargs):
        return ("deny", None)

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
        async for event in loop.run_turn(
            [ChatMessage(role="user", content="use the tool")], ask=ask_deny
        ):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].output == "Permission denied: denied by the user."


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
    note = turn_complete.new_messages[-1].content
    assert "Reached the guardrail limit" in note
    # Actionable, not just a dead end: names the setting and where to change it.
    assert "max_tool_calls_per_turn" in note
    assert "guardrails.toml" in note
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


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_default_max_tool_iterations_stops_the_turn(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    rounds = [
        httpx.Response(200, content=_sse(*_single_tool_call_round(f"call_{i}"))) for i in range(30)
    ]
    rounds.append(httpx.Response(200, content=_sse(*_final_text_chunks("done"))))
    route.side_effect = rounds

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)
    # Isolate the iteration cap: disable the (separate) per-turn tool-call guardrail.
    guardrails = GuardrailsConfig(max_tool_calls_per_turn=0)
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
            max_tool_iterations=25,
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="loop forever")]):
            events.append(event)

    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    note = turn_complete.new_messages[-1].content
    assert "Reached the max tool-call iteration limit" in note
    # Actionable, not just a dead end: names the setting and where to change it.
    assert "max_tool_iterations" in note
    assert "PCLI_MAX_TOOL_ITERATIONS" in note
    # Stopped exactly at the cap: never reached the 26th round or the final one.
    assert route.call_count == 25


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_max_tool_iterations_none_is_unlimited(tmp_path: Path):
    """local-api mode passes max_tool_iterations=None: even far more rounds
    than the historic default of 25 should run to completion uninterrupted."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    rounds = [
        httpx.Response(200, content=_sse(*_single_tool_call_round(f"call_{i}"))) for i in range(30)
    ]
    rounds.append(httpx.Response(200, content=_sse(*_final_text_chunks("done"))))
    route.side_effect = rounds

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)
    guardrails = GuardrailsConfig(max_tool_calls_per_turn=0)  # local-api disables this too
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
            max_tool_iterations=None,
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="loop a lot")]):
            events.append(event)

    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    assert turn_complete.new_messages[-1].content == "done"
    assert route.call_count == 31  # all 30 tool-call rounds plus the final text round


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_denies_a_non_plan_mode_safe_tool_when_ctx_plan_mode_is_true(tmp_path: Path):
    """The dispatch-time backstop: even if a non-plan_mode_safe tool is
    (deliberately, here) still present in the registry, ctx.plan_mode=True
    must deny it — proving the backstop isn't just relying on the registry
    having been filtered."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_first_two_tool_call_chunks())),
        httpx.Response(200, content=_sse(*_final_text_chunks("done"))),
    ]

    registry = ToolRegistry()
    registry.register(ECHO_TOOL)  # plan_mode_safe=False (the default)
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
                sandbox=sandbox,
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                plan_mode=True,
            ),
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="use the tool")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "plan mode" in tool_results[0].output
    assert sandbox.calls == []  # never reached the tool handler


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_allows_a_plan_mode_safe_tool_when_ctx_plan_mode_is_true(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_first_two_tool_call_chunks())),
        httpx.Response(200, content=_sse(*_final_text_chunks("done"))),
    ]

    plan_safe_echo = ToolSpec(
        name="echo_tool",
        description="Echoes text back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_echo_handler,
        needs_permission=False,
        plan_mode_safe=True,
    )
    registry = ToolRegistry()
    registry.register(plan_safe_echo)
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(),
                guardrails=permission_manager.guardrails,
                cwd=tmp_path,
                plan_mode=True,
            ),
        )
        events = []
        async for event in loop.run_turn([ChatMessage(role="user", content="use the tool")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is False
    assert tool_results[0].output == "echoed: hi"


@pytest.mark.asyncio
async def test_set_tool_registry_takes_effect_on_the_next_run_turn(tmp_path: Path):
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            tool_registry=None,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )
        assert loop._tool_registry is None

        registry = ToolRegistry()
        registry.register(ECHO_TOOL)
        loop.set_tool_registry(registry)

        assert loop._tool_registry is registry
        tools = loop._tool_registry.to_openai_tools()
        assert tools[0].function["name"] == "echo_tool"


@pytest.mark.asyncio
async def test_set_max_tool_iterations_takes_effect_on_the_next_run_turn(tmp_path: Path):
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
            max_tool_iterations=25,
        )
        assert loop._max_tool_iterations == 25

        loop.set_max_tool_iterations(5)
        assert loop._max_tool_iterations == 5

        loop.set_max_tool_iterations(None)  # local-api mode: unlimited
        assert loop._max_tool_iterations is None


@pytest.mark.asyncio
async def test_set_max_response_tokens_takes_effect_on_the_next_run_turn(tmp_path: Path):
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )
        assert loop._max_response_tokens is None

        loop.set_max_response_tokens(3000)
        assert loop._max_response_tokens == 3000

        loop.set_max_response_tokens(None)
        assert loop._max_response_tokens is None


@pytest.mark.asyncio
@respx.mock
async def test_run_turn_sends_the_configured_max_response_tokens(tmp_path: Path):
    """End-to-end: the value set via set_max_response_tokens actually
    reaches the outbound chat-completions request, the mechanism
    compute_max_response_tokens (cost/context.py) exists to feed."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(*_final_text_chunks()))
    )
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )
        loop.set_max_response_tokens(2048)

        async for _event in loop.run_turn([ChatMessage(role="user", content="hi")]):
            pass

    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_set_temperature_takes_effect_on_the_next_run_turn(tmp_path: Path):
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )
        assert loop._temperature is None

        loop.set_temperature(0.7)
        assert loop._temperature == 0.7

        loop.set_temperature(None)
        assert loop._temperature is None


@pytest.mark.asyncio
@respx.mock
async def test_run_turn_sends_the_configured_temperature(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(*_final_text_chunks()))
    )
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
        )
        loop.set_temperature(0.3)

        async for _event in loop.run_turn([ChatMessage(role="user", content="hi")]):
            pass

    sent = json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0.3


@pytest.mark.asyncio
async def test_set_artifact_threshold_chars_takes_effect_on_the_next_tool_result(tmp_path: Path):
    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(
            client,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=FakeSandbox(), guardrails=permission_manager.guardrails, cwd=tmp_path
            ),
            artifact_threshold_chars=4000,
        )
        assert loop._artifact_threshold_chars == 4000

        loop.set_artifact_threshold_chars(8000)
        assert loop._artifact_threshold_chars == 8000


@pytest.mark.asyncio
@respx.mock
async def test_reasoning_content_passes_through_but_is_never_folded_into_message_content(
    tmp_path: Path,
):
    """Reasoning/"thinking" model output (delta.reasoning_content) must
    reach the caller as ReasoningDelta events (chat.py renders it
    separately) but must never end up concatenated into the assistant
    message's actual content — that field is what gets replayed back to
    the model on the next turn, and reasoning traces aren't meant to be."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "hmm, "}, "finish_reason": None}]},
                {"choices": [{"delta": {"reasoning_content": "let's see"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "42"}, "finish_reason": "stop"}]},
            ),
        ),
    ]

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(client)
        events = [e async for e in loop.run_turn([ChatMessage(role="user", content="what is it")])]

    reasoning_texts = [e.text for e in events if e.kind == "reasoning_delta"]
    assert reasoning_texts == ["hmm, ", "let's see"]

    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    assert turn_complete.new_messages[-1].content == "42"  # reasoning text isn't mixed in


@pytest.mark.asyncio
@respx.mock
async def test_reasoning_only_response_ends_the_turn_with_no_content_and_no_tool_calls(
    tmp_path: Path,
):
    """The real-world failure this whole feature addresses: a model that
    spends its entire response reasoning and never transitions to `content`
    or a tool call. run_turn still ends the turn cleanly (content=None,
    no tool_calls) rather than hanging or erroring — it's chat.py's job to
    notice new_messages[-1] carries nothing visible and tell the user, with
    the reasoning (now at least captured) shown as the only clue why."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "thinking a lot"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
        ),
    ]

    async with GatewayClient(_settings()) as client:
        loop = AgentLoop(client)
        events = [e async for e in loop.run_turn([ChatMessage(role="user", content="do it")])]

    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    assert len(turn_complete.new_messages) == 1
    assert turn_complete.new_messages[0].content is None
    assert turn_complete.new_messages[0].tool_calls is None
    assert [e.text for e in events if e.kind == "reasoning_delta"] == ["thinking a lot"]
    assert route.call_count == 1  # no infinite retry loop on an empty-but-valid response


# --- "purpose" argument stripping ---


@pytest.mark.asyncio
@respx.mock
async def test_purpose_argument_never_reaches_the_tool_handler(tmp_path: Path):
    """"purpose" only exists in the advertised schema (ToolSpec.to_openai_tool)
    for the model's own benefit - AgentLoop must pop it before validation
    and before the handler ever sees it, so it can never leak into (for
    example) a toolbox tool's CLI-flag auto-builder."""
    seen_arguments: list[dict] = []

    async def _spy_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        seen_arguments.append(arguments)
        return ToolResult(output=f"echoed: {arguments['text']}")

    spy_tool = ToolSpec(
        name="echo_tool",
        description="Echoes text back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_spy_handler,
        needs_permission=False,
    )

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
                                            "name": "echo_tool",
                                            "arguments": '{"text": "hi", "purpose": "saying hi"}',
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
        httpx.Response(200, content=_sse(*_final_text_chunks("done"))),
    ]

    registry = ToolRegistry()
    registry.register(spy_tool)
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
        async for event in loop.run_turn([ChatMessage(role="user", content="use the tool")]):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert tool_results[0].is_error is False
    assert tool_results[0].output == "echoed: hi"  # succeeded despite the schema not knowing "purpose"
    assert len(seen_arguments) == 1
    assert "purpose" not in seen_arguments[0]  # never leaked into the handler
    assert seen_arguments[0] == {"text": "hi"}

    # The persisted assistant message's raw tool_call arguments JSON is left
    # untouched (purpose survives there for context_pruning.py to extract
    # later) - only the ephemeral parsed-dict copy had it popped.
    turn_complete = next(e for e in events if isinstance(e, TurnCompleteEvent))
    assistant_message = turn_complete.new_messages[0]
    assert "purpose" in assistant_message.tool_calls[0].function.arguments


def test_to_openai_tool_injects_purpose_without_mutating_the_original_schema():
    original_parameters = dict(ECHO_TOOL.parameters)

    tool_def = ECHO_TOOL.to_openai_tool()

    assert "purpose" in tool_def.function["parameters"]["properties"]
    # Deliberately optional, not required - see to_openai_tool's docstring.
    assert "purpose" not in tool_def.function["parameters"]["required"]
    # The original ToolSpec.parameters dict is never mutated - repeated
    # calls must keep producing the same result, and jsonschema validation
    # against tool.parameters (in AgentLoop._dispatch_tool_call) must never
    # see "purpose" show up as if it were a real declared argument.
    assert ECHO_TOOL.parameters == original_parameters
    assert "purpose" not in ECHO_TOOL.parameters["properties"]

    tool_def_again = ECHO_TOOL.to_openai_tool()
    assert tool_def.function["parameters"] == tool_def_again.function["parameters"]
