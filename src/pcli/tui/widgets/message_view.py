"""Scrollable conversation history widget with streaming append support."""

from __future__ import annotations

import time

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

_ROLE_LABELS = {
    "user": "You",
    "assistant": "pcli",
    "system": "System",
    "tool": "Tool",
    "shell": "Shell",
}

# Caps how often a streaming message repaints. Re-rendering on every single
# token (unthrottled) is more redraws/sec than some terminals (e.g. VTE-based
# ones on Linux) can cleanly keep up with, and manifests as flicker/ghosted
# duplicate frames. 20fps is smooth to read and gentle on any terminal.
_MIN_REFRESH_INTERVAL_S = 1 / 20


class MessageView(VerticalScroll):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current: Static | None = None
        self._current_role: str = "assistant"
        self._current_text = ""
        self._last_refresh = 0.0
        self._refresh_pending = False

    def add_message(self, role: str, text: str = "") -> Static:
        widget = Static(classes=f"message message-{role}")
        self.mount(widget)
        self._current = widget
        self._current_role = role
        self._current_text = text
        widget.update(self._render_message(role, text))
        self.scroll_end(animate=False)
        self._last_refresh = time.monotonic()
        return widget

    def append_to_last(self, fragment: str) -> None:
        if self._current is None:
            self.add_message("assistant", fragment)
            return
        self._current_text += fragment
        if time.monotonic() - self._last_refresh >= _MIN_REFRESH_INTERVAL_S:
            self._flush()
        elif not self._refresh_pending:
            self._refresh_pending = True
            self.set_timer(_MIN_REFRESH_INTERVAL_S, self._flush)

    def _flush(self) -> None:
        self._refresh_pending = False
        self._last_refresh = time.monotonic()
        if self._current is None:
            return
        self._current.update(self._render_message(self._current_role, self._current_text))
        self.scroll_end(animate=False)

    def finish_streaming(self) -> None:
        self._flush()  # ensure the last throttled fragment(s) are actually shown
        self._current = None
        self._current_text = ""

    @staticmethod
    def _render_message(role: str, text: str) -> Group:
        label = _ROLE_LABELS.get(role, role)
        return Group(Text(label, style="bold"), Markdown(text or ""))
