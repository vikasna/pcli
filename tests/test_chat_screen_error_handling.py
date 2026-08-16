"""Coverage for what happens when a turn fails with a GatewayError:
previously nothing was logged and the session was never saved, so a turn
that failed on its very first message left literally no trace anywhere —
not in pcli.log (nothing wrote to it), not in the session file (only a
successful turn triggered a save). Both are fixed here."""

import logging
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.models import Message
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


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
    return screen, session, store


@pytest.mark.asyncio
@respx.mock
async def test_gateway_error_is_logged_and_session_is_saved(tmp_path: Path, caplog):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, content=b'{"error": "unauthorized"}')
    )

    screen, session, store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None  # on_mount succeeded

        screen._session.messages.append(Message(role="user", content="hello"))
        with caplog.at_level(logging.ERROR, logger="pcli.tui.screens.chat"):
            screen._stream_response()
            for _ in range(10):
                await pilot.pause()

        # Logged with a traceback (not silently swallowed).
        assert any("Gateway error" in r.message for r in caplog.records)
        assert any(r.exc_info is not None for r in caplog.records)

        # And actually persisted to disk, unlike before — the user's
        # message that triggered the failed attempt isn't lost.
        reloaded = store.load(session.id)
        assert any(m.role == "user" and m.content == "hello" for m in reloaded.messages)


@pytest.mark.asyncio
@respx.mock
async def test_gateway_error_still_shows_in_the_message_view(tmp_path: Path):
    """The existing on-screen error notice must keep working — this is
    additive logging/persistence, not a replacement for it."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, content=b'{"error": "unauthorized"}')
    )

    screen, _session, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        screen._session.messages.append(Message(role="user", content="hello"))
        screen._stream_response()
        for _ in range(10):
            await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "Gateway error" in message_view._current_text


def test_ctrl_c_is_not_declared_as_a_chat_screen_binding():
    """Regression test for a discovered dead binding: Textual's own App
    reserves ctrl+c as a "press ctrl+q to quit" hint (App.action_help_quit)
    rather than quitting directly, and its system-level binding for that
    key always wins over a same-key Screen-level binding — so a previous
    ("ctrl+c", "quit", "Quit") entry here never actually fired. Only ctrl+q
    (Textual's own default, unrelated to anything pcli declares) quits."""
    keys = [b.key if hasattr(b, "key") else b[0] for b in ChatScreen.BINDINGS]
    assert "ctrl+c" not in keys
