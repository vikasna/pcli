"""Coverage for `!!!command` (real-terminal handoff via App.suspend()).

The actual handoff (a child process genuinely getting a working TTY) can't
be exercised under Textual's headless test driver — its `can_suspend` is
False, so `App.suspend()` always raises `SuspendNotSupported` there. That's
exactly the fallback path these tests verify; the happy path needs manual
verification in a real terminal (see the plan's Verification section)."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path) -> ChatScreen:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="")
    return ChatScreen(settings, session=session, store=store)


@pytest.mark.asyncio
async def test_bare_triple_bang_shows_usage(tmp_path: Path):
    screen = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._run_interactive_shell("")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "Usage: !!!<command>" in message_view._current_text


@pytest.mark.asyncio
async def test_suspend_not_supported_shows_graceful_message(tmp_path: Path):
    """Under the headless test driver, App.suspend() always raises
    SuspendNotSupported — verified directly against Textual's own source
    (HeadlessDriver.can_suspend is False)."""
    screen = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._run_interactive_shell("echo hi")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "isn't supported in this terminal environment" in message_view._current_text


@pytest.mark.asyncio
async def test_triple_bang_dispatches_to_interactive_not_passthrough(tmp_path: Path):
    """!!! must be checked before ! / !! in on_input_submitted, and never
    reach _run_shell_passthrough (which would try to pipe/capture output
    instead of handing off a real terminal)."""
    screen = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        input_box = screen.query_one("#input-box")
        input_box.value = "!!!echo hi"
        await pilot.press("enter")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        # Reached _run_interactive_shell's "handing off" message (or, since
        # suspend isn't supported headlessly, the graceful fallback) —
        # either way, proof it took the !!! path, not the piped one (which
        # would show "$ echo hi\nhi\n[exit_code=0]" instead).
        assert message_view._current_role == "system"
        assert "isn't supported in this terminal environment" in message_view._current_text
