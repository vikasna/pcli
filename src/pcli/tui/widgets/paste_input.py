"""Input subclass making paste (however it reaches the app) respect
expand_full_paste, instead of Textual's built-in always-first-line-only
behavior.

Two independent paths can deliver a paste, and both needed fixing:

1. **Terminal-intercepted paste.** Most terminals (Windows Terminal, xterm,
   GNOME Terminal, ...) intercept Shift+Insert (and middle-click, and
   Ctrl+Shift+V) themselves and deliver the clipboard through the
   already-enabled bracketed-paste ANSI channel — Textual turns that into an
   `events.Paste` message, which `Input._on_paste` handles by taking only
   `event.text.splitlines()[0]`. This is the path that fires in practice on
   most setups, and overriding `_on_paste` here is what actually matters.
2. **Literal keystroke.** Any terminal that instead passes Shift+Insert
   through as a plain keystroke leaves Textual with nothing bound to it by
   default (Input.BINDINGS has ctrl+v, not shift+insert) — plus Textual's
   own ctrl+v (Input.action_paste) only reads text copied *within* the app
   (App.clipboard's docstring says explicitly it doesn't track the OS
   clipboard), which can't help here either. The explicit shift+insert
   binding below reads the OS clipboard directly via pyperclip as a
   fallback for this case.

Both paths funnel into the same `_apply_pasted_text`. By default
(`expand_full_paste=False`, e.g. the sessions-import path field) only the
first line is kept, matching Textual's own prior behavior — appropriate for
fields that are conceptually single-value. With `expand_full_paste=True`
(the main chat input), the *whole* paste is kept: since Input can only ever
display one line, a multi-line paste is shown as a compact "[Pasted N
lines]" placeholder while composing, and the real full text is substituted
back in at submit time via `consume_pending_paste()` — the placeholder is a
display affordance only, never the actual message content.
"""

from __future__ import annotations

from typing import ClassVar

import pyperclip
from textual import events
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

    def _on_paste(self, event: events.Paste) -> None:
        """Overrides Input's own bracketed-paste handler — see module
        docstring: this is the path that fires for most terminals'
        Shift+Insert (and middle-click, Ctrl+Shift+V, ...).

        Textual dispatches "_on_<event>"-named handlers from *every* class
        in the MRO independently (see MessagePump._get_dispatch_methods) —
        this is cooperative layering, not standard method-override
        semantics, so merely defining _on_paste here does not stop
        Input._on_paste from *also* running right after (which would
        insert its own first-line text a second time, right after ours).
        event.prevent_default() is what actually suppresses it — the
        dispatch loop checks message._no_default_action before walking
        further up the MRO."""
        if event.text:
            self._apply_pasted_text(event.text)
        event.prevent_default()
        event.stop()

    def action_paste_from_os_clipboard(self) -> None:
        """Explicit shift+insert binding, for terminals that pass it
        through as a literal keystroke instead of intercepting it
        themselves (in which case _on_paste above never fires)."""
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException as exc:
            self.notify(f"Couldn't read the system clipboard: {exc}", severity="error")
            return
        if text:
            self._apply_pasted_text(text)

    def _apply_pasted_text(self, text: str) -> None:
        lines = text.splitlines()
        if not lines:
            return

        if not self._expand_full_paste or len(lines) <= 1:
            line = lines[0]
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
