"""Small, dependency-free text helpers shared across layers (TUI widgets,
agent/activity tracking, ...) that shouldn't have to import from each other
just to reuse a one-line string helper."""

from __future__ import annotations


def truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
