"""On-disk persistence for the global user-memory store (memory/models.py) -
mirrors tools/agent_tools_store.py's shape (a single JSON file under
data_dir(), atomic-written, loaded once and mutated in place) rather than
session/store.py's per-session-directory layout, since this is one global
file shared across every session, not per-session state."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from pcli.config.paths import memory_file
from pcli.memory.models import MemoryCategory, MemoryEntry, MemorySource, MemoryStore


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def read_memory() -> MemoryStore:
    path = memory_file()
    if not path.exists():
        return MemoryStore()
    return MemoryStore.model_validate_json(path.read_text(encoding="utf-8"))


def write_memory(store: MemoryStore) -> None:
    _atomic_write(memory_file(), store.model_dump_json(indent=2))


def add_entry(
    content: str,
    *,
    category: MemoryCategory,
    source: MemorySource,
    max_entries: int,
) -> MemoryEntry:
    """Appends a new entry, or - a deliberately simple, predictable check,
    not fuzzy matching - refreshes an existing entry in the same category
    whose content matches case-insensitively, rather than growing a pile of
    near-duplicates every time the same fact gets re-derived. Evicts the
    oldest source='derived' entry first (never 'explicit' - the user asked
    for that one directly) once adding would exceed max_entries."""
    store = read_memory()
    content = content.strip()

    for existing in store.entries:
        if existing.category == category and existing.content.strip().lower() == content.lower():
            existing.updated_at = datetime.now(UTC)
            if source == "explicit":
                existing.source = "explicit"
            write_memory(store)
            return existing

    entry = MemoryEntry(category=category, content=content, source=source)
    store.entries.append(entry)

    while len(store.entries) > max_entries:
        derived_indices = [i for i, e in enumerate(store.entries) if e.source == "derived"]
        if not derived_indices:
            break  # every remaining entry is explicit - stop evicting rather than touch those
        oldest = min(derived_indices, key=lambda i: store.entries[i].created_at)
        del store.entries[oldest]

    write_memory(store)
    return entry


def remove_entry(entry_id: str) -> bool:
    """Removes one entry by its full id. Returns whether anything was
    removed - the /memory forget <id> command resolves a short (last-4-char)
    id to a full one before calling this, so callers here always deal in
    full ids, not the abbreviated form users type."""
    store = read_memory()
    before = len(store.entries)
    store.entries = [e for e in store.entries if e.id != entry_id]
    if len(store.entries) == before:
        return False
    write_memory(store)
    return True


def clear_memory() -> None:
    write_memory(MemoryStore())
