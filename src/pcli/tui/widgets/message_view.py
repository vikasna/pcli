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

# Tool name -> the argument holding its "payload" (real, unescaped multi-line
# text worth syntax-highlighting on its own) rather than a short scalar like
# a path. edit_file is handled separately since it has two such arguments
# (old_string/new_string). Anything not listed here just gets its whole
# arguments dict pretty-printed as indented JSON instead.
_CODE_ARG_BY_TOOL = {
    "write_file": "content",
    "run_shell": "command",
    "run_shell_background": "command",
}

# A tool call rendered larger than this (in characters of its formatted body)
# is collapsed by default, same threshold spirit as tool *results* — small,
# common calls (read_file, grep, list_dir, ...) stay visible inline, only
# genuinely large payloads (a big write_file/edit_file, a long script) get
# tucked behind a click.
_LARGE_TOOL_CALL_THRESHOLD_CHARS = 500


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


def _guess_lexer(path: str | None, code: str, default: str = "text") -> str:
    if path:
        try:
            return Syntax.guess_lexer(path, code=code)
        except Exception:  # noqa: BLE001, S110 - lexer guessing is cosmetic, never fatal
            pass
    return default


def _format_tool_call_body(tool_name: str, arguments: dict[str, Any]) -> tuple[RenderableType, int]:
    """Returns (renderable, char_count) — char_count drives the collapse
    threshold in add_tool_call, measured from the actual formatted content
    (code/JSON), not the raw arguments JSON string, so it reflects what the
    user would actually see."""
    path = arguments.get("path") if isinstance(arguments.get("path"), str) else None

    if tool_name == "edit_file" and "old_string" in arguments and "new_string" in arguments:
        remaining = {k: v for k, v in arguments.items() if k not in ("old_string", "new_string")}
        old_code = str(arguments["old_string"])
        new_code = str(arguments["new_string"])
        lexer = _guess_lexer(path, old_code)
        parts: list[RenderableType] = []
        if remaining:
            parts.append(Text(_format_remaining_args(remaining), style="dim"))
        parts.append(Text("- old_string", style="bold red"))
        parts.append(Syntax(old_code, lexer, word_wrap=True, background_color="default"))
        parts.append(Text("+ new_string", style="bold green"))
        parts.append(Syntax(new_code, lexer, word_wrap=True, background_color="default"))
        return Group(*parts), len(old_code) + len(new_code)

    code_arg = _CODE_ARG_BY_TOOL.get(tool_name)
    if code_arg and code_arg in arguments and isinstance(arguments[code_arg], str):
        remaining = {k: v for k, v in arguments.items() if k != code_arg}
        code = arguments[code_arg]
        default_lexer = "bash" if tool_name.startswith("run_shell") else "text"
        lexer = _guess_lexer(path, code, default=default_lexer)
        parts = []
        if remaining:
            parts.append(Text(_format_remaining_args(remaining), style="dim"))
        parts.append(Syntax(code, lexer, word_wrap=True, background_color="default"))
        return Group(*parts), len(code)

    if not arguments:
        return Text("(no arguments)", style="dim"), 0
    pretty = json.dumps(arguments, indent=2, ensure_ascii=False)
    return Syntax(pretty, "json", word_wrap=True, background_color="default"), len(pretty)


def _format_remaining_args(remaining: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in remaining.items())


class MessageView(VerticalScroll):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current: Static | None = None
        self._current_role: str = "assistant"
        self._current_text = ""
        self._last_refresh = 0.0
        self._refresh_pending = False

    def _scroll_end_if_at_bottom(self, was_at_bottom: bool) -> None:
        """Auto-scroll is "stick to bottom", not "force to bottom": if the
        user had already scrolled up (e.g. to read an expanded "Thinking" or
        tool-result panel while a turn keeps streaming below it), further
        content must not drag them back down and fight that scroll. Only
        resumes once they scroll back to the bottom themselves. Callers must
        capture is_vertical_scroll_end *before* mounting/updating content —
        doing it after would always read as "not at bottom" once the
        content just grew."""
        if was_at_bottom:
            self.scroll_end(animate=False)

    def add_message(self, role: str, text: str = "") -> Static:
        was_at_bottom = self.is_vertical_scroll_end
        widget = Static(classes=f"message message-{role}")
        self.mount(widget)
        self._current = widget
        self._current_role = role
        self._current_text = text
        widget.update(self._render_message(role, text))
        self._scroll_end_if_at_bottom(was_at_bottom)
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
        was_at_bottom = self.is_vertical_scroll_end
        self._current.update(self._render_message(self._current_role, self._current_text))
        self._scroll_end_if_at_bottom(was_at_bottom)

    def add_tool_call(self, tool_name: str, arguments_json: str, *, purpose: str | None = None) -> None:
        """The "→ tool_name(...)" preview shown as soon as a call is
        dispatched, before its result is known. Known code-bearing arguments
        (write_file's content, run_shell's command, edit_file's old/new
        strings) are syntax-highlighted with real line breaks instead of the
        raw, single-line-escaped JSON string; anything else falls back to
        pretty-printed JSON. Large calls collapse behind a click, mirroring
        add_tool_result — small/common calls (read_file, grep, ...) stay
        visible inline."""
        was_at_bottom = self.is_vertical_scroll_end
        try:
            arguments = json.loads(arguments_json or "{}")
        except (json.JSONDecodeError, ValueError):
            arguments = None
        if not isinstance(arguments, dict):
            # Malformed/non-object arguments (shouldn't normally happen -
            # AgentLoop's own dispatch would separately reject these) - fall
            # back to the raw string rather than crashing the render.
            self.add_message("tool", f"→ {tool_name}({arguments_json})")
            self._scroll_end_if_at_bottom(was_at_bottom)
            return

        # "purpose" is shown on its own line below, not mixed into the
        # argument listing/highlighting.
        arguments = {k: v for k, v in arguments.items() if k != "purpose"}
        body_renderable, char_count = _format_tool_call_body(tool_name, arguments)

        if char_count > _LARGE_TOOL_CALL_THRESHOLD_CHARS:
            parts: list[RenderableType] = []
            if purpose:
                parts.append(Text(purpose, style="italic"))
            parts.append(body_renderable)
            title = f"→ {tool_name}(...) — {char_count:,} char(s)"
            body = Static(Group(*parts), classes="tool-result-body")
            collapsible = Collapsible(body, title=title, collapsed=True, classes="tool-call-collapsible")
            self.mount(collapsible)
        else:
            parts = [Text(f"→ {tool_name}", style="bold")]
            if purpose:
                parts.append(Text(purpose, style="italic"))
            parts.append(body_renderable)
            widget = Static(Group(*parts), classes="message message-tool")
            self.mount(widget)
        self._scroll_end_if_at_bottom(was_at_bottom)

    def add_tool_result(self, tool_name: str, output: str, *, is_error: bool) -> None:
        """Tool results render as a collapsed-by-default Collapsible (full
        content available on demand) rather than a fixed-length preview —
        the previous view-layer truncation hid content that was often
        already fully available in memory at this point. Formatting is
        handled by _format_tool_output; this never touches self._current
        (no ongoing streaming state to track for a tool result)."""
        was_at_bottom = self.is_vertical_scroll_end
        status_icon = "✗" if is_error else "✓"
        title = f"{status_icon} {tool_name} — {len(output):,} char(s)"
        body = Static(_format_tool_output(output), classes="tool-result-body")
        classes = "tool-result-collapsible" + (" error" if is_error else "")
        collapsible = Collapsible(body, title=title, collapsed=True, classes=classes)
        self.mount(collapsible)
        self._scroll_end_if_at_bottom(was_at_bottom)

    def add_reasoning(self, text: str) -> None:
        """A reasoning/"thinking" model's chain-of-thought (delta.reasoning_content
        — see llm/streaming.py), rendered collapsed by default: it's the
        model's internal monologue, not its actual answer, and can run to
        thousands of tokens. One-shot render like add_tool_result (never
        streamed token-by-token into the DOM) — this is secondary content,
        and the "busy" spinner already covers the in-progress case."""
        if not text.strip():
            return
        was_at_bottom = self.is_vertical_scroll_end
        title = f"\U0001f9e0 Thinking — {len(text):,} char(s)"
        body = Static(Text(text, no_wrap=False, overflow="fold"), classes="tool-result-body")
        collapsible = Collapsible(body, title=title, collapsed=True, classes="reasoning-collapsible")
        self.mount(collapsible)
        self._scroll_end_if_at_bottom(was_at_bottom)

    def finish_streaming(self) -> None:
        self._flush()  # ensure the last throttled fragment(s) are actually shown
        self._current = None
        self._current_text = ""

    @staticmethod
    def _render_message(role: str, text: str) -> Group:
        label = _ROLE_LABELS.get(role, role)
        return Group(Text(label, style="bold"), Markdown(text or ""))
