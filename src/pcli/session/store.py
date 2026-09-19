"""On-disk CRUD for sessions, plus a fast index for listing without loading
every session file in full."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from pcli.config.paths import sessions_dir
from pcli.session.models import Session, SessionIndexEntry


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


class SessionNotFoundError(Exception):
    pass


class SessionStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or sessions_dir()
        self._index_path = self._base_dir / "index.json"

    def session_dir(self, session_id: str) -> Path:
        return self._base_dir / session_id

    def blobs_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "blobs"

    def _session_file(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def new_session(
        self, *, model: str = "", gateway_base_url: str = "", working_dir: str | None = None
    ) -> Session:
        session = Session(model=model, gateway_base_url=gateway_base_url, working_dir=working_dir)
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        session.touch()
        _atomic_write(
            self._session_file(session.id), session.model_dump_json(indent=2)
        )
        self._upsert_index(session)

    def load(self, session_id: str) -> Session:
        path = self._session_file(session_id)
        if not path.exists():
            raise SessionNotFoundError(session_id)
        return Session.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, session_id: str) -> None:
        import shutil

        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)
        index = self._read_index()
        index.pop(session_id, None)
        self._write_index(index)

    def list_index(self) -> list[SessionIndexEntry]:
        index = self._read_index()
        entries = [SessionIndexEntry.model_validate(v) for v in index.values()]
        return sorted(entries, key=lambda e: e.updated_at, reverse=True)

    def prune_empty_sessions(self, *, exclude_session_ids: Iterable[str] = ()) -> int:
        """Deletes every persisted session with zero messages. new_session()
        saves eagerly the moment a fresh session is created (before the
        leading system prompt is even appended in ChatScreen.__init__), so
        closing pcli without ever sending a message leaves a useless,
        never-touched entry cluttering /sessions — this is the only way a
        session ends up with message_count == 0, since messages are only
        ever appended, never removed (even a turn that fails outright still
        persists the system prompt + the user's message first). Returns how
        many were pruned."""
        exclude = set(exclude_session_ids)
        pruned = 0
        for entry in self.list_index():
            if entry.message_count == 0 and entry.id not in exclude:
                self.delete(entry.id)
                pruned += 1
        return pruned

    def write_blob(self, session_id: str, invocation_id: str, content: str) -> str:
        blob_name = f"{invocation_id}.txt"
        self.write_blob_named(session_id, blob_name, content)
        return blob_name

    def write_blob_named(self, session_id: str, blob_name: str, content: str) -> None:
        """Writes a blob under an exact filename (no '.txt' appended) — used
        by the importer to restore blobs under their original blob_name."""
        blob_path = self.blobs_dir(session_id) / blob_name
        _atomic_write(blob_path, content)

    def read_blob(self, session_id: str, blob_name: str) -> str:
        blob_path = self.blobs_dir(session_id) / blob_name
        return blob_path.read_text(encoding="utf-8")

    def _read_index(self) -> dict[str, dict]:
        if not self._index_path.exists():
            return {}
        return json.loads(self._index_path.read_text(encoding="utf-8"))

    def _write_index(self, index: dict[str, dict]) -> None:
        _atomic_write(self._index_path, json.dumps(index, indent=2, default=str))

    def _upsert_index(self, session: Session) -> None:
        index = self._read_index()
        entry = SessionIndexEntry(
            id=session.id,
            title=session.derive_title(),
            updated_at=session.updated_at,
            model=session.model,
            message_count=len(session.messages),
            total_cost_usd=session.cost.session_total_usd,
        )
        index[session.id] = json.loads(entry.model_dump_json())
        self._write_index(index)
