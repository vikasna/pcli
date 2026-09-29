"""Coverage for /theme (view or live-switch the TUI's Textual color theme,
persisted to config.toml the same way /rename persists a session title).
Textual's own theme registration/application at startup is covered
separately in test_tui_app.py (real PcliApp, since that's where
tui/themes.py's vim-* themes actually get registered - a bare test _HostApp
here only has Textual's own builtins available)."""

import tomllib
from pathlib import Path

import pytest
from textual.app import App

from pcli.config.paths import config_file
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


def _make_screen(tmp_path: Path, **settings_overrides):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


@pytest.mark.asyncio
async def test_theme_with_no_argument_lists_available_themes_and_marks_active(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/theme")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        text = message_view._current_text
        assert "textual-dark" in text
        assert f"`{app.theme}` (active)" in text
        assert "Usage: /theme <name>" in text


@pytest.mark.asyncio
async def test_theme_switches_live_and_persists(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/theme gruvbox")
        await pilot.pause()

        # The set-confirmation is a toast (App._notifications), not a
        # transcript message - see chat.py's self.notify(...) call and the
        # "Split system notices" design note; unlike the listing branches
        # below, which stay in the transcript since they dump the full
        # theme list.
        assert any("Theme set to 'gruvbox'." in n.message for n in app._notifications)
        assert app.theme == "gruvbox"
        assert screen._settings.ui_theme == "gruvbox"

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["ui_theme"] == "gruvbox"


@pytest.mark.asyncio
async def test_theme_unknown_name_reports_the_available_list_instead_of_failing(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        theme_before = app.theme

        screen._handle_command("/theme not-a-real-theme")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        text = message_view._current_text
        assert "Unknown theme 'not-a-real-theme'" in text
        assert "Available themes" in text
        assert app.theme == theme_before  # unchanged
