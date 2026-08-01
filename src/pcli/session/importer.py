"""Imports a session exported by session/export.py.

Two deliberate safety choices, both documented in the plan:
- `permission_grants` is dropped unless `restore_grants=True` is passed
  explicitly, so an imported session can't silently carry "always allow
  shell" grants onto a new machine.
- The session always gets a *new* id on import (the original id is kept in
  metadata for traceability) so importing never collides with / overwrites
  an existing local session.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pcli.session.export import FORMAT_NAME, FORMAT_VERSION
from pcli.session.models import Session
from pcli.session.store import SessionStore


class SessionImportError(Exception):
    pass


# Each entry migrates a raw session dict from its key version to key+1.
# e.g. {1: _migrate_v1_to_v2} once format_version 2 exists.
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def _read_envelope(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":  # gzip magic bytes
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def import_session(
    path: Path,
    *,
    store: SessionStore | None = None,
    restore_grants: bool = False,
) -> Session:
    store = store or SessionStore()
    envelope = _read_envelope(path)

    if envelope.get("format") != FORMAT_NAME:
        raise SessionImportError(f"Not a pcli session export: {path}")

    version = envelope.get("format_version")
    if not isinstance(version, int):
        raise SessionImportError(f"Missing/invalid format_version in {path}")
    if version > FORMAT_VERSION:
        raise SessionImportError(
            f"{path} was exported by a newer pcli (format_version={version}); "
            f"this pcli supports up to {FORMAT_VERSION}. Upgrade pcli to import it."
        )

    session_data = envelope["session"]
    while version < FORMAT_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            raise SessionImportError(f"No migration registered from format_version {version}")
        session_data = migrate(session_data)
        version += 1

    original_id = session_data.pop("id", None)
    if not restore_grants:
        session_data["permission_grants"] = []

    session = Session.model_validate(session_data)
    session.metadata["imported_from_id"] = original_id
    session.metadata["imported_from_file"] = str(path)

    blobs: dict[str, str] = envelope.get("blobs", {})
    for invocation in session.tool_invocations:
        if invocation.full_result_ref and invocation.full_result_ref in blobs:
            store.write_blob_named(
                session.id, invocation.full_result_ref, blobs[invocation.full_result_ref]
            )

    store.save(session)
    return session
