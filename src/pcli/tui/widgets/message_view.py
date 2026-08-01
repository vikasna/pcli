"""Scrollable conversation history widget with streaming append support."""

from __future__ import annotations

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


class MessageView(VerticalScroll):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current: Static | None = None
        self._current_role: str = "assistant"
        self._current_text = ""

    def add_message(self, role: str, text: str = "") -> Static:
        widget = Static(classes=f"message message-{role}")
        self.mount(widget)
        self._current = widget
        self._current_role = role
        self._current_text = text
        widget.update(self._render_message(role, text))
        self.scroll_end(animate=False)
        return widget

    def append_to_last(self, fragment: str) -> None:
        if self._current is None:
            self.add_message("assistant", fragment)
            return
        self._current_text += fragment
        self._current.update(self._render_message(self._current_role, self._current_text))
        self.scroll_end(animate=False)

    def finish_streaming(self) -> None:
        self._current = None
        self._current_text = ""

    @staticmethod
    def _render_message(role: str, text: str) -> Group:
        label = _ROLE_LABELS.get(role, role)
        return Group(Text(label, style="bold"), Markdown(text or ""))
