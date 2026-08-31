"""Shared "what should a paste look like in the input box" decision, used by
both PasteInput (Input-based) and ChatInput (TextArea-based) — the two
widgets differ in how they actually insert text (Input.replace() takes flat
character offsets; TextArea.replace() takes row/col Locations), but the
placeholder/expansion behavior itself is one implementation, not two
diverging copies.

By default (expand_full_paste=False, e.g. the sessions-import path field)
only the first line is kept, matching Textual's own Input._on_paste
behavior — appropriate for fields that are conceptually single-value. With
expand_full_paste=True (the main chat input), the *whole* paste is kept: a
multi-line paste is shown as a compact "[Pasted N lines]" placeholder while
composing (never the raw multi-line text — this is what keeps a large
paste from growing an auto-growing input box), and the real full text is
substituted back in at submit time via PendingPaste.consume().
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PasteDecision:
    text_to_insert: str
    pending_full_text: str | None = None
    pending_marker: str | None = None


def decide_paste(text: str, *, expand_full_paste: bool) -> PasteDecision | None:
    """Returns None for an empty clipboard/paste (nothing to insert)."""
    lines = text.splitlines()
    if not lines:
        return None

    if not expand_full_paste or len(lines) <= 1:
        line = lines[0]
        if not line:
            return None
        return PasteDecision(text_to_insert=line)

    marker = f"[Pasted {len(lines)} lines]"
    return PasteDecision(text_to_insert=marker, pending_full_text=text, pending_marker=marker)


class PendingPaste:
    """Tracks a still-composing placeholder marker and the full text it
    stands in for, until consume() expands it back at submit time (or the
    marker gets edited away, in which case consume() is a no-op)."""

    def __init__(self) -> None:
        self._text: str | None = None
        self._marker: str | None = None

    def set(self, marker: str, full_text: str) -> None:
        self._marker = marker
        self._text = full_text

    def consume(self, text: str) -> str:
        """Expands a still-present placeholder marker back to the full
        text it stands in for. Always clears the pending state — a
        submitted turn resets it either way, so an edited-away or
        already-used marker can't leak into a later, unrelated paste."""
        pending_text, marker = self._text, self._marker
        self._text = None
        self._marker = None
        if pending_text is not None and marker is not None and marker in text:
            return text.replace(marker, pending_text)
        return text
