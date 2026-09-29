"""Coverage for Esc+Esc turn cancellation: a double-press of Escape within
_ESCAPE_DOUBLE_PRESS_WINDOW_S cancels the in-flight turn via
self.workers.cancel_group(self, "agent-turn") — Textual's built-in worker
cancellation, which raises asyncio.CancelledError inside the running
coroutine at its next await point. A single press only shows a hint."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}),
    )


def _make_screen(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        # Not what this file tests, and would otherwise add a spurious
        # "Couldn't auto-detect..." system message to every fresh mount.
        context_limit_auto_detect_enabled=False,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


async def _start_a_slow_turn(screen, pilot, release_event: asyncio.Event):
    async def side_effect(request):
        await release_event.wait()
        return _text_response("reply")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=side_effect)

    field = screen.query_one(ChatInput)
    field.focus()
    field.text = "hello"
    await pilot.press("enter")
    for _ in range(5):
        await pilot.pause()
    assert screen._turn_in_progress is True


@pytest.mark.asyncio
async def test_escape_with_no_turn_in_progress_is_a_noop(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen.action_cancel_turn()
        await pilot.pause()

        assert message_view._current_text == ""  # nothing was shown - a true no-op


@pytest.mark.asyncio
@respx.mock
async def test_single_escape_shows_hint_and_does_not_cancel(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        release_event = asyncio.Event()
        await _start_a_slow_turn(screen, pilot, release_event)

        screen.action_cancel_turn()
        await pilot.pause()

        assert screen._turn_in_progress is True  # not cancelled
        assert any("Press Esc again" in n.message for n in app._notifications)

        # Let the turn actually finish so the test doesn't leak a worker.
        release_event.set()
        for _ in range(20):
            await pilot.pause()
        assert screen._turn_in_progress is False


@pytest.mark.asyncio
@respx.mock
async def test_escape_escape_within_the_window_cancels_the_turn(tmp_path: Path):
    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        release_event = asyncio.Event()
        await _start_a_slow_turn(screen, pilot, release_event)

        screen.action_cancel_turn()
        screen.action_cancel_turn()  # back-to-back: well within the window
        for _ in range(20):
            await pilot.pause()

        assert screen._turn_in_progress is False
        message_view = screen.query_one(MessageView)
        assert "Turn cancelled." in message_view._current_text

        # The turn never completed, so nothing was appended for it.
        assert not any(m.role == "assistant" and m.content == "reply" for m in session.messages)


@pytest.mark.asyncio
@respx.mock
async def test_escape_escape_outside_the_window_does_not_cancel(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        release_event = asyncio.Event()
        await _start_a_slow_turn(screen, pilot, release_event)

        import pcli.tui.screens.chat as chat_module

        screen.action_cancel_turn()  # first press
        # Rewind the recorded press time as if it happened long ago, rather
        # than monkeypatching time.monotonic() globally (which Textual's own
        # internals also call for scheduling, unrelated to this test).
        screen._last_escape_at -= chat_module._ESCAPE_DOUBLE_PRESS_WINDOW_S + 1.0
        screen.action_cancel_turn()  # "second" press, but too late to count
        await pilot.pause()

        assert screen._turn_in_progress is True  # still not cancelled - too slow

        release_event.set()
        for _ in range(20):
            await pilot.pause()


@pytest.mark.asyncio
@respx.mock
async def test_escape_escape_drops_queued_followups_and_clears_the_queue(tmp_path: Path):
    """Regression test for a bug caught during design review: cancelling the
    worker running _stream_response's while-loop kills the loop that would
    have consulted the queue - if it were left non-empty, a LATER, unrelated
    turn's loop-check would spuriously run an extra turn for stale queued
    text. Cancellation must clear it."""
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        release_event = asyncio.Event()
        await _start_a_slow_turn(screen, pilot, release_event)

        # Simulate two messages having been queued while the turn above was
        # in flight (on_chat_input_submitted's queueing path).
        screen._queued_followups = ["a followup", "another followup"]

        screen.action_cancel_turn()
        screen.action_cancel_turn()
        for _ in range(20):
            await pilot.pause()

        assert screen._queued_followups == []
        message_view = screen.query_one(MessageView)
        assert "2 queued follow-up messages not sent." in message_view._current_text
