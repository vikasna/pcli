"""Coverage for agent/headless.py: the turn-driving logic behind `pcli run`
(see test_cli_run.py for the CLI command's own argument/exit-code wiring) -
new_headless_session's system-prompt construction, and run_headless_task's
turn loop including the auto-continue-on-truncation behavior it
deliberately replicates from ChatScreen._run_one_turn (see agent/loop.py's
TurnCompleteEvent.response_truncated)."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from pcli.agent.headless import (
    _MAX_CONSECUTIVE_AUTO_CONTINUES,
    new_headless_session,
    run_headless_task,
)
from pcli.agent.prompt import PLAN_MODE_REINFORCEMENT
from pcli.agent.runtime import build_agent_runtime, build_permission_manager
from pcli.config.settings import Settings
from pcli.memory.store import add_entry
from pcli.session.store import SessionStore


def _settings(**overrides) -> Settings:
    defaults = {
        "gateway_base_url": "http://fake-gateway.test/v1",
        "gateway_api_key": "test-key",
        "default_model": "fake-model",
        "sandbox_backend": "subprocess",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(
            {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
        ),
    )


# --- new_headless_session ---


def test_new_headless_session_builds_a_system_prompt(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, _settings(), tmp_path)

    assert len(session.messages) == 1
    assert session.messages[0].role == "system"
    assert session.working_dir == str(tmp_path)


def test_new_headless_session_includes_memory_when_enabled(tmp_path: Path):
    add_entry("Works as a backend Python developer", category="profile", source="derived", max_entries=40)
    store = SessionStore(base_dir=tmp_path / "sessions")

    session = new_headless_session(store, _settings(), tmp_path)

    assert "Works as a backend Python developer" in session.messages[0].content


def test_new_headless_session_omits_memory_when_disabled(tmp_path: Path):
    add_entry("Works as a backend Python developer", category="profile", source="derived", max_entries=40)
    store = SessionStore(base_dir=tmp_path / "sessions")

    session = new_headless_session(store, _settings(memory_enabled=False), tmp_path)

    assert "Works as a backend Python developer" not in session.messages[0].content


# --- run_headless_task ---


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_happy_path(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("All done.")
    )

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)
    progress: list[str] = []

    try:
        result = await run_headless_task(
            "do the thing",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
            on_progress=progress.append,
        )
    finally:
        await runtime.client.aclose()

    assert result.final_text == "All done."
    assert result.terminated_early is False
    assert result.truncations_exhausted is False
    assert any("do the thing" in line for line in progress)
    assert session.cost.total_tokens == 60

    reloaded = store.load(session.id)
    contents = [m.content for m in reloaded.messages]
    assert "do the thing" in contents
    assert "All done." in contents


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_auto_continues_a_truncated_response(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _text_response("Let me start:", finish_reason="length"),
        _text_response("Now finished."),
    ]

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)

    try:
        result = await run_headless_task(
            "do the thing",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
        )
    finally:
        await runtime.client.aclose()

    assert route.call_count == 2
    assert result.final_text == "Now finished."
    assert result.truncations_exhausted is False
    assert any(m.content == "Continue." for m in session.messages)


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_stops_after_the_truncation_cap(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _text_response(f"partial {i}", finish_reason="length")
        for i in range(_MAX_CONSECUTIVE_AUTO_CONTINUES + 1)
    ]

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)

    try:
        result = await run_headless_task(
            "do the thing",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
        )
    finally:
        await runtime.client.aclose()

    assert route.call_count == _MAX_CONSECUTIVE_AUTO_CONTINUES + 1
    assert result.truncations_exhausted is True
    continue_count = sum(1 for m in session.messages if m.content == "Continue.")
    assert continue_count == _MAX_CONSECUTIVE_AUTO_CONTINUES


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_permission_not_pre_granted_denies_the_tool_call(tmp_path: Path):
    """No `ask` callback in headless mode - anything not already "Allow
    Always"-granted must be denied, not hang or crash (see permissions/
    manager.py's own fail-closed-without-ask behavior)."""
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
                                        "function": {"name": "run_shell", "arguments": '{"command": "echo hi"}'},
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
        _text_response("Couldn't run the command."),
    ]

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)

    try:
        await run_headless_task(
            "run a command",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
        )
    finally:
        await runtime.client.aclose()

    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "Permission denied" in tool_messages[0].content
    assert route.call_count == 2

    # Regression coverage for the actual gap this feature fixes: headless
    # runs used to never append to session.tool_invocations at all (only
    # ChatScreen did) - a denied tool call must still be recorded.
    assert len(session.tool_invocations) == 1
    assert session.tool_invocations[0].tool_name == "run_shell"
    assert session.tool_invocations[0].status == "error"


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_uses_a_supplied_ask_callback(tmp_path: Path):
    """A caller that does have a way to ask (namely `pcli telegram`, see
    telegram/daemon.py) can supply `ask` - the tool call is then gated
    through it exactly like an interactive TUI session, instead of always
    failing closed the way the no-`ask` default above does."""
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
                                        "function": {"name": "run_shell", "arguments": '{"command": "echo hi"}'},
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
        _text_response("Ran it."),
    ]

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)
    asked: list[str] = []

    async def ask(tool_name: str, arguments: dict, risk_description: str):
        asked.append(tool_name)
        return "allow", "once"

    try:
        await run_headless_task(
            "run a command",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
            ask=ask,
        )
    finally:
        await runtime.client.aclose()

    assert asked == ["run_shell"]
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "Permission denied" not in tool_messages[0].content
    assert "hi" in tool_messages[0].content

    # Same gap-fix regression coverage as the denied-call test above, for
    # the successful-call path.
    assert len(session.tool_invocations) == 1
    assert session.tool_invocations[0].tool_name == "run_shell"
    assert session.tool_invocations[0].status == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_records_audit_entries_when_enabled(tmp_path: Path):
    """audit_mode_enabled threads through to both PermissionManager (via
    build_permission_manager) and the tool-call recording added to close
    the gap above - a headless run with it on should produce a permission_
    decision entry and a tool_call entry, hash-chained together."""
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
                                        "function": {"name": "run_shell", "arguments": '{"command": "echo hi"}'},
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
        _text_response("Ran it."),
    ]

    settings = _settings(audit_mode_enabled=True)
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)

    async def ask(tool_name: str, arguments: dict, risk_description: str):
        return "allow", "once"

    try:
        await run_headless_task(
            "run a command",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
            ask=ask,
        )
    finally:
        await runtime.client.aclose()

    kinds = [e.kind for e in session.audit_log]
    assert kinds == ["permission_decision", "tool_call"]
    assert session.audit_log[0].detail["decision"] == "allow"
    assert session.audit_log[1].detail["tool_name"] == "run_shell"
    assert session.audit_log[1].prev_hash == session.audit_log[0].entry_hash


# --- plan_mode ---


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_plan_mode_injects_reinforcement_not_persisted(tmp_path: Path):
    """Mirrors test_chat_screen_plan_mode.py's own equivalent test - the
    same ephemeral, per-turn reinforcement (agent/prompt.py's
    PLAN_MODE_REINFORCEMENT) chat.py injects, shared rather than
    duplicated so the wording can't drift between the TUI and the
    Telegram daemon, which is the actual caller that needed this."""
    captured_requests: list[bytes] = []

    def _capture(request):
        captured_requests.append(request.content)
        return _text_response("ok")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=_capture)

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)

    try:
        await run_headless_task(
            "look around",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
            plan_mode=True,
        )
    finally:
        await runtime.client.aclose()

    sent = json.loads(captured_requests[0])
    sent_contents = [m.get("content", "") for m in sent["messages"]]
    assert any(PLAN_MODE_REINFORCEMENT in c for c in sent_contents if c)
    assert not any(m.content and PLAN_MODE_REINFORCEMENT in m.content for m in session.messages)


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_plan_mode_denies_a_non_plan_mode_safe_tool_call(tmp_path: Path):
    """Proves ctx.plan_mode (threaded through make_tool_context) is what
    denies this, not just registry filtering - no tool_registry override
    is passed here, so the model's own call is only ever blocked by
    AgentLoop's dispatch-time backstop in agent/loop.py."""
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
                                            "name": "write_file",
                                            "arguments": json.dumps(
                                                {"path": "x.txt", "content": "hi"}
                                            ),
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
        _text_response("understood"),
    ]

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)

    try:
        await run_headless_task(
            "create a file",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
            plan_mode=True,
        )
    finally:
        await runtime.client.aclose()

    tool_message = next(m for m in session.messages if m.role == "tool")
    assert "not available in plan mode" in tool_message.content
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
@respx.mock
async def test_run_headless_task_tool_registry_override_hides_tools_from_the_model(tmp_path: Path):
    """Confirms the tool_registry param is actually threaded into
    AgentLoop's own tool list sent to the gateway, not just accepted and
    ignored - the primary plan-mode mechanism (the model never even sees
    a disallowed tool), same as ChatScreen._effective_tool_registry."""
    captured_requests: list[bytes] = []

    def _capture(request):
        captured_requests.append(request.content)
        return _text_response("done")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=_capture)

    settings = _settings()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = new_headless_session(store, settings, tmp_path)
    runtime = await build_agent_runtime(settings, tmp_path)
    filtered = runtime.tool_registry.filtered(lambda t: t.plan_mode_safe)

    try:
        await run_headless_task(
            "look around",
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=tmp_path,
            store=store,
            tool_registry=filtered,
        )
    finally:
        await runtime.client.aclose()

    sent = json.loads(captured_requests[0])
    tool_names = {t["function"]["name"] for t in sent.get("tools", [])}
    assert "read_file" in tool_names
    assert "write_file" not in tool_names
