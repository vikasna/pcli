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

    import pcli.tools._nested_agent as nested_agent_module

    nested_agent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

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
        nested_agent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

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

    import pcli.tools._nested_agent as nested_agent_module

    nested_agent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

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
        nested_agent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

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

    import pcli.tools._nested_agent as nested_agent_module

    nested_agent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

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
        nested_agent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert len(captured_registries[0]) == 1
    assert "echo_tool" in captured_registries[0]


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_always_includes_write_todos_even_if_the_model_omits_it(
    tmp_path: Path,
):
    """The calling model's own allowed_tools list can't be relied on to
    consistently include write_todos (the exact model behind the session
    this was built from omitted it more often than not) - write_todos is
    let through regardless, the same structural guarantee spawn_subagent
    itself already gets (never spawnable from within a subagent) - so the
    "use write_todos for multi-step work" discipline
    (tools/_nested_agent.py's _TODO_DISCIPLINE) doesn't silently go missing
    just because the model forgot to list it."""
    from pcli.tools.builtin.todo_tool import WRITE_TODOS

    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools._nested_agent as nested_agent_module

    nested_agent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = _make_registry_with_echo_and_subagent()
        registry.register(WRITE_TODOS)

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            # Deliberately omits "write_todos" - this is the exact scenario
            # that must still work.
            await SPAWN_SUBAGENT.handler(
                {"task": "do something", "allowed_tools": ["echo_tool"]}, ctx
            )
    finally:
        nested_agent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert "write_todos" in captured_registries[0]
    assert "echo_tool" in captured_registries[0]

    sent = json.loads(route.calls.last.request.content)
    system_message = sent["messages"][0]["content"]
    assert "write_todos to lay out a plan" in system_message


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

    import pcli.tools._nested_agent as nested_agent_module

    nested_agent_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

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
        nested_agent_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

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


def _tool_call_round(call_id: str) -> list[dict]:
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {"name": "echo_tool", "arguments": "{}"},
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
async def test_spawn_subagent_marks_hitting_the_iteration_cap_as_an_error(tmp_path: Path):
    """Regression coverage for the real failure this was built for: a
    subagent that never finishes (keeps calling tools past its cap) must
    come back to the parent as an error with a DID NOT FINISH marker, not a
    normal-looking result a weak model can mistake for success."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_round(f"call_{i}"))) for i in range(5)
    ]

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = replace(_make_ctx(tmp_path, registry, client, permission_manager), subagent_max_iterations=2)
        result = await SPAWN_SUBAGENT.handler({"task": "echo forever"}, ctx)

    assert result.is_error is True
    assert "DID NOT FINISH" in result.output
    assert "INCOMPLETE" in result.output
    assert route.call_count == 2  # stopped exactly at the cap


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_requested_max_iterations_is_capped_by_ctx_subagent_max_iterations(
    tmp_path: Path,
):
    """The model can ask for more iterations via the tool's own
    max_iterations argument, but it's still clamped to the configured
    ceiling (ctx.subagent_max_iterations) - never silently ignored, but
    never unbounded either."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse(*_tool_call_round(f"call_{i}"))) for i in range(5)
    ]

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = replace(_make_ctx(tmp_path, registry, client, permission_manager), subagent_max_iterations=3)
        result = await SPAWN_SUBAGENT.handler(
            {"task": "echo forever", "max_iterations": 100}, ctx
        )

    assert result.is_error is True
    assert route.call_count == 3  # capped at ctx.subagent_max_iterations, not the requested 100


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_normal_completion_is_not_marked_as_error(tmp_path: Path):
    """A subagent finishing well within its cap must NOT get the DID NOT
    FINISH treatment - only an actual forced cutoff should."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("The answer is 42.")
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = replace(_make_ctx(tmp_path, registry, client, permission_manager), subagent_max_iterations=25)
        result = await SPAWN_SUBAGENT.handler({"task": "What is the answer?"}, ctx)

    assert result.is_error is False
    assert "DID NOT FINISH" not in result.output


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


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_notes_high_context_usage(tmp_path: Path, monkeypatch):
    """A subagent's own conversation has no compaction of its own - if it
    ends up using most of the model's context window, that must be surfaced
    to the parent as a real explanation, not silently dropped. Isolated from
    the real user's context_limits.toml the same way test_context_tracking.py
    does, so "fake-model" deterministically falls back to the built-in
    128_000 default regardless of what's on the machine running this test."""
    import pcli.cost.context as context_module

    monkeypatch.setattr(context_module, "context_limits_file", lambda: tmp_path / "context_limits.toml")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response(
            "Done.",
            usage={"prompt_tokens": 110_000, "completion_tokens": 1000, "total_tokens": 111_000},
        )
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        result = await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)

    assert result.is_error is False  # finished normally - this is informational, not a failure
    assert "reached 87%" in result.output
    assert "128,000-token context limit" in result.output


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_does_not_note_low_context_usage(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    monkeypatch.setattr(context_module, "context_limits_file", lambda: tmp_path / "context_limits.toml")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response(
            "Done.",
            usage={"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000},
        )
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        result = await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)

    assert "context limit" not in result.output


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_system_prompt_includes_core_discipline(tmp_path: Path):
    """Regression coverage for a real observed session: a subagent made a
    single tool call and reported a large multi-file task as fully
    complete. The subagent's own system prompt never inherited
    BASE_SYSTEM_PROMPT's verification discipline (it fully replaces the
    main loop's prompt rather than extending it) - run_nested_agent now
    appends a shared discipline block to every nested agent regardless."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("done")
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)

    sent = json.loads(route.calls.last.request.content)
    system_message = sent["messages"][0]["content"]
    assert "only actions you actually took via tool calls" in system_message
    assert "list_dir/read_file it" in system_message


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_system_prompt_includes_environment_section(tmp_path: Path):
    """Regression coverage for a real gap: a subagent's system prompt fully
    replaces the main loop's rather than extending it, so it previously got
    no OS/shell/path facts at all and independently defaulted to the same
    Linux/bash training-data assumption the main loop's own environment
    section exists to prevent."""
    from pcli.agent.prompt import environment_section

    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("done")
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)

    sent = json.loads(route.calls.last.request.content)
    system_message = sent["messages"][0]["content"]
    assert environment_section() in system_message
    assert "relative to the working directory" in system_message


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_omits_todo_discipline_when_write_todos_not_available(tmp_path: Path):
    """write_todos isn't in this test registry's tool set at all - telling
    the subagent to use it would be actively wrong guidance."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("done")
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)

    sent = json.loads(route.calls.last.request.content)
    system_message = sent["messages"][0]["content"]
    assert "write_todos" not in system_message


@pytest.mark.asyncio
@respx.mock
async def test_spawn_subagent_includes_todo_discipline_when_write_todos_is_available(tmp_path: Path):
    from pcli.tools.builtin.todo_tool import WRITE_TODOS

    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("done")
    )

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry_with_echo_and_subagent()
    registry.register(WRITE_TODOS)

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        await SPAWN_SUBAGENT.handler({"task": "do something"}, ctx)

    sent = json.loads(route.calls.last.request.content)
    system_message = sent["messages"][0]["content"]
    assert "write_todos to lay out a plan" in system_message
