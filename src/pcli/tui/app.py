"""Root Textual application."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from pcli.config.settings import Settings, get_settings
from pcli.session.models import Session
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.themes import VIM_THEMES

_STYLES_PATH = Path(__file__).parent / "styles" / "pcli.tcss"


class PcliApp(App):
    CSS_PATH = str(_STYLES_PATH)
    TITLE = "pcli"

    def __init__(self, settings: Settings | None = None, *, session: Session | None = None) -> None:
        super().__init__()
        self._settings = settings or get_settings()
        self._initial_session = session

    def on_mount(self) -> None:
        for theme in VIM_THEMES:
            self.register_theme(theme)
        # settings.ui_theme is free-text (hand-edited config.toml, or a
        # stale value from a pcli version whose theme set has since
        # changed) - applying an unknown name would raise straight out of
        # on_mount, so this is checked rather than trusted. Falls back to
        # whatever App.theme already defaults to (textual-dark) rather than
        # failing the whole app over one bad setting.
        if self._settings.ui_theme in self.available_themes:
            self.theme = self._settings.ui_theme
        self.push_screen(ChatScreen(self._settings, session=self._initial_session))
