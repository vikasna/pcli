"""Coverage for automatic continuation after a truncated response: a real
reported bug where the model's reply got cut off by the token limit
mid-task ("Let me implement X:" with no tool call following, because there
was no room left to make one) and pcli just silently ended the turn there,
leaving the user to notice and type "continue" themselves. ChatScreen._run_
one_turn now does that automatically whenever AgentLoop reports
TurnCompleteEvent.response_truncated (see test_agent_loop.py for that
detection's own unit coverage), capped at _MAX_CONSECUTIVE_AUTO_CONTINUES
consecutive attempts."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import _MAX_CONSECUTIVE_AUTO_CONTINUES, ChatScreen
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


def _response(text: str, *, finish_reason: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(
            {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]},
        ),
    )


def _truncated(text: str) -> httpx.Response:
    return _response(text, finish_reason="length")


def _stopped(text: str) -> httpx.Response:
    return _response(text, finish_reason="stop")


def _make_screen(tmp_path: Path, **settings_overrides) -> tuple[ChatScreen, object]:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        auto_compact_enabled=False,  # unrelated concern - keep these tests focused
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


async def _submit(pilot, screen: ChatScreen, text: str) -> None:
    field = screen.query_one(ChatInput)
    field.focus()
    field.text = text
    await pilot.press("enter")
    for _ in range(20):
        await pilot.pause()


@pytest.mark.asyncio
@respx.mock
async def test_truncated_response_triggers_automatic_continuation(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _truncated("Let me implement. Starting with embedding_store.py:"),
        _stopped("Done, wrote the file."),
    ]

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, screen, "build the thing")

        assert route.call_count == 2
        roles_and_content = [(m.role, m.content) for m in session.messages if m.role in ("user", "assistant")]
        assert roles_and_content == [
            ("user", "build the thing"),
            ("assistant", "Let me implement. Starting with embedding_store.py:"),
            ("user", "Continue."),
            ("assistant", "Done, wrote the file."),
        ]
        assert screen._consecutive_truncations == 0  # reset after the normal completion


@pytest.mark.asyncio
@respx.mock
async def test_normal_response_does_not_trigger_continuation(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_stopped("all done")
    )

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, screen, "hello")

        assert route.call_count == 1
        assert "Continue." not in [m.content for m in session.messages]


@pytest.mark.asyncio
@respx.mock
async def test_consecutive_truncations_stop_after_the_cap(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    # One more truncated response than the cap allows - the extra one must
    # never actually be requested (auto-continue stops before making it).
    route.side_effect = [_truncated(f"partial {i}") for i in range(_MAX_CONSECUTIVE_AUTO_CONTINUES + 1)]

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, screen, "build the thing")

        # The original turn plus exactly _MAX_CONSECUTIVE_AUTO_CONTINUES retries.
        assert route.call_count == _MAX_CONSECUTIVE_AUTO_CONTINUES + 1
        continue_count = sum(1 for m in session.messages if m.content == "Continue.")
        assert continue_count == _MAX_CONSECUTIVE_AUTO_CONTINUES

        message_view = screen.query_one(MessageView)
        assert "stopping automatic continuation" in message_view._current_text
        assert screen._consecutive_truncations == 0  # reset so a later attempt gets a fresh budget


@pytest.mark.asyncio
@respx.mock
async def test_sending_a_new_message_resets_the_truncation_counter(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _truncated("first attempt:"),
        _stopped("finished first task"),
        _truncated("second attempt:"),
        _stopped("finished second task"),
    ]

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, screen, "first task")
        assert screen._consecutive_truncations == 0

        await _submit(pilot, screen, "second task")
        assert screen._consecutive_truncations == 0
        assert route.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_auto_continue_notice_is_rendered_in_the_transcript(tmp_path: Path, monkeypatch):
    """Unlike the "Continue." message (also appended to session.messages,
    checked elsewhere), the "continuing automatically" notice is a
    message_view-only system notice - same convention as compaction's own
    "Compacted N message(s)..." notice, so it's never in session.messages
    to check there. Spies on MessageView.add_message directly (its plain
    text argument) rather than trying to extract text back out of the Rich
    renderable message_view._current_text/query(...) would give, since
    add_message's later calls (the "Continue." message, the next turn's
    reply) would already have overwritten _current_text by the time the
    turn settles."""
    calls: list[tuple[str, str]] = []
    original_add_message = MessageView.add_message

    def _spy(self, role: str, text: str = "") -> object:
        calls.append((role, text))
        return original_add_message(self, role, text)

    monkeypatch.setattr(MessageView, "add_message", _spy)

    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [_truncated("cut off here:"), _stopped("done")]

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, screen, "do the task")

        assert any(
            role == "system" and "continuing automatically" in text for role, text in calls
        )
