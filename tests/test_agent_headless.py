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
