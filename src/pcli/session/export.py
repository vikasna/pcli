"""Exports a session to a portable, self-contained .pcli-session.json[.gz] file.

The gateway API key is never part of the Session model in the first place, so
there is nothing to redact there. `permission_grants` travels with the export
for fidelity, but importer.py drops it by default (see importer.py docstring)
so an imported session can't silently grant shell access on the new machine.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from pcli import __version__
from pcli.session.models import Session
from pcli.session.store import SessionStore

FORMAT_NAME = "pcli-session"
FORMAT_VERSION = 1


def export_session(
    session: Session,
    out_path: Path,
    *,
    store: SessionStore | None = None,
    use_gzip: bool | None = None,
) -> Path:
    store = store or SessionStore()
    if use_gzip is None:
        use_gzip = out_path.suffix == ".gz"

    blobs: dict[str, str] = {}
    for invocation in session.tool_invocations:
        if invocation.full_result_ref:
            blobs[invocation.full_result_ref] = store.read_blob(
                session.id, invocation.full_result_ref
            )

    envelope = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "pcli_version": __version__,
        "session": json.loads(session.model_dump_json()),
        "blobs": blobs,
    }

    body = json.dumps(envelope, indent=2).encode("utf-8")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if use_gzip:
        out_path.write_bytes(gzip.compress(body))
    else:
        out_path.write_bytes(body)
    return out_path
