"""Coverage for queueing a follow-up message submitted while a turn is
still in flight: previously ChatScreen._stream_response was one of 7
methods on this screen all sharing Textual's default exclusive worker
group, so starting *any* of them (including a second call to
_stream_response itself) silently cancelled whichever one was already
running instead of queueing or running after it — e.g. submitting a second
chat message mid-turn killed the first turn outright. Fixed by giving
_stream_response its own dedicated worker group ("agent-turn") and having
on_chat_input_submitted queue a follow-up (via _turn_in_progress /
_queued_followups) instead of starting a second worker in that group.

A second, later bug (also covered here): the queued text used to be
spliced into session.messages *immediately*, on the same event-loop turn
the keystroke arrived — before the in-flight turn's own assistant reply had
been appended (that only happens once turn_complete fires). So the queued
message landed *before* that reply in the conversation history, corrupting
turn order. Fixed by holding queued text in _queued_followups (not
session.messages) until the in-flight turn actually finishes."""

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
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


@pytest.mark.asyncio
@respx.mock
async def test_second_message_submitted_mid_turn_is_queued_not_cancelled(tmp_path: Path):
    release_first = asyncio.Event()
    calls: list[str] = []

    async def side_effect(request):
        calls.append("call")
        if len(calls) == 1:
            await release_first.wait()
            return _text_response("first reply")
        return _text_response("second reply")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=side_effect)

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "first"
        await pilot.press("enter")

        # Let the worker start and reach the (currently blocked) gateway call.
        for _ in range(5):
            await pilot.pause()
        assert screen._turn_in_progress is True

        # Submitted while the first turn is still in flight: must be queued,
        # not start a second worker that would cancel the first, and NOT yet
        # appear in session.messages (that would land it before the first
        # turn's own reply, corrupting order — the actual bug this covers).
        field.text = "second"
        await pilot.press("enter")
        await pilot.pause()

        assert screen._queued_followups == ["second"]
        user_messages = [m for m in session.messages if m.role == "user"]
        assert [m.content for m in user_messages] == ["first"]

        # Release the first (still in-flight) gateway response and let both
        # turns run to completion.
        release_first.set()
        for _ in range(20):
            await pilot.pause()

        assert screen._turn_in_progress is False
        assert screen._queued_followups == []
        # Correct interleaving: "second" (appended only once the first turn
        # finished) sits after "first reply", not before it.
        roles_and_content = [(m.role, m.content) for m in session.messages if m.role != "system"]
        assert roles_and_content == [
            ("user", "first"),
            ("assistant", "first reply"),
            ("user", "second"),
            ("assistant", "second reply"),
        ]
        assert len(calls) == 2  # the first turn was never restarted/duplicated


@pytest.mark.asyncio
@respx.mock
async def test_other_exclusive_worker_no_longer_cancels_an_in_flight_turn(tmp_path: Path):
    """Regression test for the actual root cause: _stream_response used to
    share Textual's default exclusive worker group with every other
    @work(exclusive=True) method on this screen (toolbox commands, /models,
    /export, /compact, ...) — starting any of them cancelled whichever one
    was already running. _stream_response now has its own "agent-turn"
    group, so an unrelated exclusive worker (here: /toolbox list) starting
    mid-turn must not cancel it."""
    release_first = asyncio.Event()

    async def side_effect(request):
        await release_first.wait()
        return _text_response("first reply")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=side_effect)

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "hello"
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert screen._turn_in_progress is True

        # A different exclusive worker (default group) starting mid-turn.
        screen._toolbox_list()
        await pilot.pause()

        assert screen._turn_in_progress is True  # not cancelled by the above

        release_first.set()
        for _ in range(20):
            await pilot.pause()

        assert screen._turn_in_progress is False
        assistant_messages = [m for m in session.messages if m.role == "assistant"]
        assert [m.content for m in assistant_messages] == ["first reply"]


@pytest.mark.asyncio
@respx.mock
async def test_manual_compact_is_rejected_while_a_turn_is_in_progress(tmp_path: Path):
    release_first = asyncio.Event()

    async def side_effect(request):
        await release_first.wait()
        return _text_response("first reply")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=side_effect)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "hello"
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert screen._turn_in_progress is True

        await screen._run_compaction("manual")
        await pilot.pause()
        # Toast, not a transcript message.
        notifications = [n.message.lower() for n in app._notifications]
        assert any("still working on the current turn" in m for m in notifications)

        release_first.set()
        for _ in range(20):
            await pilot.pause()

        assert screen._turn_in_progress is False
