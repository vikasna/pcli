"""ChatInput: the main chat input box — a TextArea subclass (Textual's
Input is fundamentally single-line, confirmed via its source; there's no
incremental path to multi-line from it) that:

- Submits on plain Enter, inserts a real newline on Ctrl+J or Alt+Enter
  (Ctrl+J is the primary binding: it's the raw LF byte, so it works
  identically across terminals regardless of kitty-keyboard-protocol
  support, which Shift+Enter/Alt+Enter often need to be distinguished from
  plain Enter at all).
- Grows to fit typed content, up to a max, and shrinks back down —
  but ONLY from the user's own typing. A pasted multi-line blob is
  collapsed to a one-line "[Pasted N lines]" placeholder (see
  paste_marker.py, shared with PasteInput) rather than inserted as real
  multi-line text, so pasting something huge never balloons the box —
  the real text is substituted back in at submit time via
  consume_pending_paste().
- Recalls previously-submitted messages with Up/Down, shell-style: only
  while the current draft has no newline in it (once you're actively
  editing a multi-line draft, Up/Down move the cursor within it instead,
  which is what you'd want anyway — this sidesteps having to reason about
  soft-wrapped visual rows vs. logical lines).
"""

from __future__ import annotations

from typing import ClassVar

import pyperclip
from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import TextArea

from pcli.tui.widgets.paste_marker import PendingPaste, decide_paste

_MAX_VISIBLE_LINES = 10
_BORDER_ROWS = 2  # matches the prior fixed `height: 3` = 1 content + 2 border


class ChatInput(TextArea):
    # Merges with TextArea's own BINDINGS (Textual's binding metaclass
    # merges subclass + parent, doesn't replace — confirmed by the
    # identical pattern/test in PasteInput), so this only needs to add
    # what's new, not repeat TextArea's own cursor/edit bindings.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("shift+insert", "paste_from_os_clipboard", "Paste", show=False),
    ]

    class Submitted(Message):
        """Posted when Enter is pressed (and not swallowed as a newline
        request) — mirrors Input.Submitted's shape."""

        def __init__(self, chat_input: ChatInput, value: str) -> None:
            self.chat_input = chat_input
            self.value = value
            super().__init__()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, soft_wrap=True, tab_behavior="focus", **kwargs)
        self._pending_paste = PendingPaste()
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft_before_history = ""

    def on_mount(self) -> None:
        self.styles.height = 1 + _BORDER_ROWS

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if event.key in ("ctrl+j", "alt+enter"):
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        await super()._on_key(event)

    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        # wrapped_document.height (visual row count, accounting for
        # soft-wrap) rather than document.line_count (logical "\n" count) -
        # a single long typed line that wraps across several visual rows
        # needs the box to grow just as much as an explicit newline would,
        # otherwise TextArea shows its own internal scrollbar instead.
        visual_line_count = self.wrapped_document.height
        clamped = max(1, min(visual_line_count, _MAX_VISIBLE_LINES))
        self.styles.height = clamped + _BORDER_ROWS

    def action_cursor_up(self) -> None:
        if "\n" in self.text or not self._history:
            super().action_cursor_up()
            return
        if self._history_index is None:
            self._draft_before_history = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self.text = self._history[self._history_index]
        self.move_cursor(self.document.end)

    def action_cursor_down(self) -> None:
        if "\n" in self.text or self._history_index is None:
            super().action_cursor_down()
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.text = self._history[self._history_index]
        else:
            self._history_index = None
            self.text = self._draft_before_history
        self.move_cursor(self.document.end)

    def add_to_history(self, text: str) -> None:
        """Records a just-submitted message and resets history browsing —
        called by ChatScreen right after handling a submission, regardless
        of which branch (!/  //plain text) handled it."""
        if text:
            self._history.append(text)
        self._history_index = None
        self._draft_before_history = ""

    def _on_paste(self, event: events.Paste) -> None:
        """Overrides TextArea's own bracketed-paste handler (which would
        otherwise insert the full multi-line text verbatim) — see
        paste_marker.py's module docstring: a multi-line paste must stay
        collapsed behind a one-line placeholder so it can't grow the box.

        Textual dispatches "_on_<event>"-named handlers from every class in
        the MRO independently (confirmed via PasteInput's identical fix) —
        event.prevent_default() is what stops TextArea's own _on_paste from
        also running right after."""
        if event.text:
            self._apply_pasted_text(event.text)
        event.prevent_default()
        event.stop()

    def action_paste_from_os_clipboard(self) -> None:
        """Explicit shift+insert binding, for terminals that pass it
        through as a literal keystroke instead of intercepting it
        themselves (in which case _on_paste above never fires) — see
        PasteInput's identical rationale."""
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException as exc:
            self.notify(f"Couldn't read the system clipboard: {exc}", severity="error")
            return
        if text:
            self._apply_pasted_text(text)

    def _apply_pasted_text(self, text: str) -> None:
        decision = decide_paste(text, expand_full_paste=True)
        if decision is None:
            return
        if decision.pending_marker is not None:
            self._pending_paste.set(decision.pending_marker, decision.pending_full_text)
        start, end = self.selection
        self.replace(decision.text_to_insert, start, end)

    def consume_pending_paste(self, text: str) -> str:
        return self._pending_paste.consume(text)
