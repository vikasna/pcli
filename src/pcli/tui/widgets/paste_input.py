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
(handled by Input._on_paste, which already correctly takes only the first
line), but any terminal that instead passes it through as a literal
keystroke leaves it doing nothing. This binding reads the real OS clipboard
directly via pyperclip instead, so Shift+Insert works regardless of
terminal behavior — taking only the first line, same as Input._on_paste,
since Input is single-line."""

from __future__ import annotations

from typing import ClassVar

import pyperclip
from textual.binding import Binding, BindingType
from textual.widgets import Input


class PasteInput(Input):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("shift+insert", "paste_from_os_clipboard", "Paste", show=False),
    ]

    def action_paste_from_os_clipboard(self) -> None:
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException as exc:
            self.notify(f"Couldn't read the system clipboard: {exc}", severity="error")
            return
        if not text:
            return
        line = text.splitlines()[0]
        if not line:
            return
        start, end = self.selection
        self.replace(line, start, end)
