"""Input subclass adding an explicit Shift+Insert -> paste-from-OS-clipboard
binding.

Textual's Input already binds ctrl+v to its own `paste` action
(Input.action_paste), but that only reads text copied *within* the app —
Textual's own App.clipboard docstring says explicitly it "only contains
text copied in the app, and not text copied from elsewhere in the OS." That
can't fix a "Shift+Insert does nothing" case, since the whole point of
Shift+Insert is pasting text copied from *outside* pcli (a browser, another
terminal, an editor, ...). Some terminals intercept Shift+Insert themselves
and route it through Textual's already-enabled bracketed-paste support
(handled by Input._on_paste, which only takes the first line), but any
terminal that instead passes it through as a literal keystroke leaves it
doing nothing. This binding reads the real OS clipboard directly via
pyperclip instead, so Shift+Insert works regardless of terminal behavior.

By default (`expand_full_paste=False`, e.g. the sessions-import path field)
only the first line is inserted, matching Input._on_paste's own behavior —
appropriate for fields that are conceptually single-value. With
`expand_full_paste=True` (the main chat input), the *whole* clipboard is
kept: since Input can only ever display one line, a multi-line paste is
shown as a compact "[Pasted N lines]" placeholder while composing, and the
real full text is substituted back in at submit time via
`consume_pending_paste()` — the placeholder is a display affordance only,
never the actual message content.
"""

from __future__ import annotations

from typing import ClassVar

import pyperclip
from textual.binding import Binding, BindingType
from textual.widgets import Input


class PasteInput(Input):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("shift+insert", "paste_from_os_clipboard", "Paste", show=False),
    ]

    def __init__(self, *args, expand_full_paste: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._expand_full_paste = expand_full_paste
        self._pending_paste_text: str | None = None
        self._pending_paste_marker: str | None = None

    def action_paste_from_os_clipboard(self) -> None:
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException as exc:
            self.notify(f"Couldn't read the system clipboard: {exc}", severity="error")
            return
        if not text:
            return

        lines = text.splitlines()
        if not self._expand_full_paste or len(lines) <= 1:
            line = lines[0] if lines else ""
            if not line:
                return
            start, end = self.selection
            self.replace(line, start, end)
            return

        marker = f"[Pasted {len(lines)} lines]"
        self._pending_paste_text = text
        self._pending_paste_marker = marker
        start, end = self.selection
        self.replace(marker, start, end)

    def consume_pending_paste(self, text: str) -> str:
        """Expands a still-present placeholder marker back to the full
        clipboard text it stands in for. Always clears the pending state —
        a submitted turn resets it either way, so an edited-away or
        already-used marker can't leak into a later, unrelated paste."""
        pending_text, marker = self._pending_paste_text, self._pending_paste_marker
        self._pending_paste_text = None
        self._pending_paste_marker = None
        if pending_text is not None and marker is not None and marker in text:
            return text.replace(marker, pending_text)
        return text
