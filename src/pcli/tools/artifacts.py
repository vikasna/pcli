"""The 'artifact library': large tool outputs get truncated out of the live
conversation (see AgentLoop's dispatch) and archived here instead, so they
don't get re-sent to the model on every subsequent call. The LLM can pull
one back with the fetch_artifact tool when it actually needs the detail.

Backed by the session's own blob storage rather than a separate mechanism,
so archived content persists/exports/imports exactly like everything else
in the session — an artifact_id doubles as a ToolInvocation.full_result_ref.
"""

from __future__ import annotations

from typing import Protocol

from pcli.session.store import SessionStore
from pcli.util.ids import new_id


class ArtifactStore(Protocol):
    def put(self, content: str) -> str:
        """Archives content, returning a stable artifact_id to retrieve it by."""
        ...

    def get(self, artifact_id: str) -> str | None:
        """Returns the archived content, or None if artifact_id is unknown."""
        ...


class SessionArtifactStore:
    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    def put(self, content: str) -> str:
        artifact_id = new_id("art_")
        self._store.write_blob_named(self._session_id, self.blob_name_for(artifact_id), content)
        return artifact_id

    def get(self, artifact_id: str) -> str | None:
        try:
            return self._store.read_blob(self._session_id, self.blob_name_for(artifact_id))
        except FileNotFoundError:
            return None

    @staticmethod
    def blob_name_for(artifact_id: str) -> str:
        return f"{artifact_id}.txt"
