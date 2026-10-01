"""Tamper-evident audit log: a hash-chained record of permission decisions
and tool calls, kept alongside - not instead of - Session.tool_invocations/
permission_grants. Opt-in (Settings.audit_mode_enabled), for regulated-
industry use where a user needs to prove, after the fact, what an
unattended agent actually did and that the record wasn't edited later.

The chain primitive is deliberately simple: each entry's hash covers its
own content plus the previous entry's hash, same as a git commit or a
certificate-transparency log. No new dependency (stdlib hashlib/json
only), no signing/external timestamping service - that would be real
complexity for marginal benefit over what a plain SHA-256 chain already
gives: editing, reordering, or deleting an old entry breaks the chain from
that point forward, and the break can't be silently repaired without
re-deriving every hash after it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from pcli.util.ids import new_id

if TYPE_CHECKING:
    from pcli.session.models import Session

_GENESIS_HASH = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("audit_"))
    created_at: datetime = Field(default_factory=_utcnow)
    kind: Literal["tool_call", "permission_decision"]
    summary: str
    """Human-readable one-liner, e.g. 'read_file allowed' or 'run_shell
    denied: command matched denylist entry'."""
    detail: dict[str, Any] = Field(default_factory=dict)
    """kind-specific structured payload - see append_audit_entry's callers
    (agent/runtime.py's record_tool_invocation, permissions/manager.py's
    check_with_reason) for the exact fields each kind carries."""
    prev_hash: str
    """The previous entry's entry_hash, or _GENESIS_HASH for the first
    entry in a session's chain."""
    entry_hash: str
    """sha256 over a canonical (sort_keys=True) JSON serialization of
    every field above except this one - see _compute_hash."""


@dataclass
class AuditVerification:
    valid: bool
    entry_count: int
    broken_at_index: int | None = None
    reason: str | None = None


def _hashable_payload(entry: AuditEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "created_at": entry.created_at.isoformat(),
        "kind": entry.kind,
        "summary": entry.summary,
        "detail": entry.detail,
        "prev_hash": entry.prev_hash,
    }


def _compute_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_audit_entry(
    session: Session, *, kind: Literal["tool_call", "permission_decision"], summary: str, detail: dict[str, Any]
) -> AuditEntry:
    """Appends one entry to session.audit_log, chained onto whatever's
    already there. Callers gate this behind Settings.audit_mode_enabled
    themselves (agent/runtime.py's record_tool_invocation, permissions/
    manager.py's PermissionManager) - this function always appends
    unconditionally when called, same as Session.decisions/
    tool_invocations never gate themselves either."""
    prev_hash = session.audit_log[-1].entry_hash if session.audit_log else _GENESIS_HASH
    entry = AuditEntry(kind=kind, summary=summary, detail=detail, prev_hash=prev_hash, entry_hash="")
    entry.entry_hash = _compute_hash(_hashable_payload(entry))
    session.audit_log.append(entry)
    return entry


def verify_audit_chain(session: Session) -> AuditVerification:
    """Recomputes every entry's hash from its recorded fields and checks
    prev_hash linkage - an empty log is valid (nothing to verify, not an
    error: audit_mode_enabled may simply have been off for this session)."""
    expected_prev = _GENESIS_HASH
    for index, entry in enumerate(session.audit_log):
        if entry.prev_hash != expected_prev:
            return AuditVerification(
                valid=False,
                entry_count=len(session.audit_log),
                broken_at_index=index,
                reason="prev_hash does not match the preceding entry's hash - an entry was "
                "likely reordered or deleted",
            )
        recomputed = _compute_hash(_hashable_payload(entry))
        if recomputed != entry.entry_hash:
            return AuditVerification(
                valid=False,
                entry_count=len(session.audit_log),
                broken_at_index=index,
                reason="entry_hash does not match the recomputed hash - this entry's content "
                "was modified after it was recorded",
            )
        expected_prev = entry.entry_hash
    return AuditVerification(valid=True, entry_count=len(session.audit_log))
