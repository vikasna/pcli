"""Root Textual application."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from pcli.config.settings import Settings, get_settings
from pcli.tui.screens.chat import ChatScreen

_STYLES_PATH = Path(__file__).parent / "styles" / "pcli.tcss"


class PcliApp(App):
    CSS_PATH = str(_STYLES_PATH)
    TITLE = "pcli"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self._settings = settings or get_settings()

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._settings))
