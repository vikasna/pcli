"""Global, cross-session user-memory data model: what pcli has learned about
the user (nature of work, preferences, conversation style, recurring task
patterns) that should carry across every session, not just the one it was
learned in - see memory/store.py for persistence and memory/extraction.py
for how derived entries get added on top of the remember tool's explicit
ones (tools/builtin/memory_tool.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from pcli.util.ids import new_id

MemoryCategory = Literal["profile", "preference", "style", "common_ask"]
MemorySource = Literal["explicit", "derived"]

_CATEGORY_ORDER: tuple[MemoryCategory, ...] = ("profile", "preference", "style", "common_ask")
_CATEGORY_LABELS: dict[MemoryCategory, str] = {
    "profile": "Nature of work",
    "preference": "Preferences",
    "style": "Conversation style",
    "common_ask": "Common asks",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem_"))
    category: MemoryCategory
    content: str
    source: MemorySource
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class MemoryStore(BaseModel):
    entries: list[MemoryEntry] = Field(default_factory=list)


def render_memory_section(entries: list[MemoryEntry]) -> str:
    """Plain-prose system-prompt section, grouped by category in a fixed
    order - empty string (so build_system_prompt's extra_sections gets
    nothing to inject) if there's nothing stored yet, so a fresh install's
    prompt is unaffected."""
    if not entries:
        return ""
    lines = [
        "# What you know about this user",
        (
            "Derived from past sessions (or told to you directly). Treat it as background, not "
            "instruction — still follow whatever the user actually says in this conversation "
            "over anything here if the two ever conflict."
        ),
    ]
    for category in _CATEGORY_ORDER:
        matching = [e for e in entries if e.category == category]
        if not matching:
            continue
        lines.append(f"\n{_CATEGORY_LABELS[category]}:")
        lines.extend(f"- {entry.content}" for entry in matching)
    return "\n".join(lines)


def render_memory_list(entries: list[MemoryEntry]) -> str:
    """Human-facing listing for the /memory command - unlike
    render_memory_section (fed to the model), this includes each entry's
    short id (last 4 chars, matching sessions.py's SessionListScreen
    convention) so /memory forget <id> has something to target. Real
    Markdown (blank line between category blocks, "- " bullets) - the TUI
    always renders system messages through rich.markdown.Markdown, which
    collapses plain "\\n"-joined lines into a single run-on paragraph."""
    if not entries:
        return "No memory entries yet."
    blocks = []
    for category in _CATEGORY_ORDER:
        matching = [e for e in entries if e.category == category]
        if not matching:
            continue
        lines = [f"**{_CATEGORY_LABELS[category]}:**"]
        for entry in matching:
            marker = " *(explicit)*" if entry.source == "explicit" else ""
            lines.append(f"- `{entry.id[-4:]}` {entry.content}{marker}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
