"""Scrollable conversation history widget with streaming append support."""

from __future__ import annotations

import json
import time
from typing import Any

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

_ROLE_LABELS = {
    "user": "You",
    "assistant": "pcli",
    "system": "System",
    "tool": "Tool",
    "shell": "Shell",
    "decision": "Decision",
}

# Caps how often a streaming message repaints. Re-rendering on every single
# token (unthrottled) is more redraws/sec than some terminals (e.g. VTE-based
# ones on Linux) can cleanly keep up with, and manifests as flicker/ghosted
# duplicate frames. 20fps is smooth to read and gentle on any terminal.
_MIN_REFRESH_INTERVAL_S = 1 / 20


def _format_tool_output(output: str) -> RenderableType:
    """Pretty-prints/syntax-highlights JSON tool output; otherwise renders
    verbatim as monospace text (not Markdown — tool output routinely contains
    underscores/asterisks/etc. that Markdown would misinterpret)."""
    stripped = output.strip()
    if stripped[:1] in "{[":
        parsed: Any = None
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
        if parsed is not None:
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            return Syntax(pretty, "json", word_wrap=True, background_color="default")
    return Text(output, no_wrap=False, overflow="fold")


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

    def add_tool_result(self, tool_name: str, output: str, *, is_error: bool) -> None:
        """Tool results render as a collapsed-by-default Collapsible (full
        content available on demand) rather than a fixed-length preview —
        the previous view-layer truncation hid content that was often
        already fully available in memory at this point. Formatting is
        handled by _format_tool_output; this never touches self._current
        (no ongoing streaming state to track for a tool result)."""
        status_icon = "✗" if is_error else "✓"
        title = f"{status_icon} {tool_name} — {len(output):,} char(s)"
        body = Static(_format_tool_output(output), classes="tool-result-body")
        classes = "tool-result-collapsible" + (" error" if is_error else "")
        collapsible = Collapsible(body, title=title, collapsed=True, classes=classes)
        self.mount(collapsible)
        self.scroll_end(animate=False)

    def add_reasoning(self, text: str) -> None:
        """A reasoning/"thinking" model's chain-of-thought (delta.reasoning_content
        — see llm/streaming.py), rendered collapsed by default: it's the
        model's internal monologue, not its actual answer, and can run to
        thousands of tokens. One-shot render like add_tool_result (never
        streamed token-by-token into the DOM) — this is secondary content,
        and the "busy" spinner already covers the in-progress case."""
        if not text.strip():
            return
        title = f"\U0001f9e0 Thinking — {len(text):,} char(s)"
        body = Static(Text(text, no_wrap=False, overflow="fold"), classes="tool-result-body")
        collapsible = Collapsible(body, title=title, collapsed=True, classes="reasoning-collapsible")
        self.mount(collapsible)
        self.scroll_end(animate=False)

    def finish_streaming(self) -> None:
        self._flush()  # ensure the last throttled fragment(s) are actually shown
        self._current = None
        self._current_text = ""

    @staticmethod
    def _render_message(role: str, text: str) -> Group:
        label = _ROLE_LABELS.get(role, role)
        return Group(Text(label, style="bold"), Markdown(text or ""))
