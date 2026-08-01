"""Tracks 'always allow/deny' grants (persisted to disk) and per-session
grants (in-memory only for the life of the process)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from pcli.config.paths import permissions_file


class StoredGrant(BaseModel):
    tool_name: str
    argument_pattern: str | None = None
    decision: Literal["allow", "deny"] = "allow"


class PermissionPolicy:
    def __init__(self, *, persist_path: Path | None = None) -> None:
        self._persist_path = persist_path or permissions_file()
        self._always_grants: list[StoredGrant] = self._load()
        self._session_grants: list[StoredGrant] = []

    def _load(self) -> list[StoredGrant]:
        if not self._persist_path.exists():
            return []
        raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        return [StoredGrant.model_validate(g) for g in raw]

    def _save(self) -> None:
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps([g.model_dump() for g in self._always_grants], indent=2)
        tmp_path = self._persist_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, self._persist_path)

    def remember(
        self,
        tool_name: str,
        *,
        scope: Literal["session", "always"],
        decision: Literal["allow", "deny"] = "allow",
        argument_pattern: str | None = None,
    ) -> None:
        grant = StoredGrant(
            tool_name=tool_name, argument_pattern=argument_pattern, decision=decision
        )
        if scope == "always":
            self._always_grants.append(grant)
            self._save()
        else:
            self._session_grants.append(grant)

    def check(
        self, tool_name: str, argument_pattern: str | None = None
    ) -> Literal["allow", "deny"] | None:
        for grant in (*self._session_grants, *self._always_grants):
            if grant.tool_name != tool_name:
                continue
            if grant.argument_pattern is not None and grant.argument_pattern != argument_pattern:
                continue
            return grant.decision
        return None

    def session_grants_snapshot(self) -> list[StoredGrant]:
        return list(self._session_grants)
