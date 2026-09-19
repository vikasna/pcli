"""Persisted session data model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from pcli.llm.models import ChatMessage, Role, ToolCall, Usage
from pcli.util.ids import new_id

CURRENT_SCHEMA_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Message(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg_"))
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    pruned_artifact_id: str | None = None
    """Set when this tool-role message's content has been replaced with a
    compact placeholder by agent/context_pruning.py's prune_old_tool_results
    — the original full content was archived first (retrievable via
    fetch_artifact), and this field is the idempotency guard preventing a
    message from being re-archived/re-pruned on a later call."""

    def to_chat_message(self) -> ChatMessage:
        return ChatMessage(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
            name=self.name,
        )

    @classmethod
    def from_chat_message(cls, message: ChatMessage) -> Message:
        return cls(
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            name=message.name,
        )


class ToolInvocation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("inv_"))
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    full_result_ref: str | None = None
    """Blob filename (relative to the session's blobs/ dir) holding the full,
    untruncated result, when it was too large to inline in result_summary."""
    status: Literal["ok", "error", "denied"] = "ok"
    backend: str | None = None
    duration_ms: float | None = None
    permission_decision: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class TurnCost(BaseModel):
    turn_index: int
    model: str
    usage: Usage
    cost_usd: float
    estimated: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    source: Literal["main", "subagent", "compaction", "memory"] = "main"
    """Which conversation this usage actually came from. cost/context.py's
    current_context_usage/looks_like_context_ceiling need this to find the
    last *main*-conversation entry specifically — a subagent's,
    compaction's, or memory extraction's own usage is real spend (still
    counted in SessionCost.total_tokens/session_total_usd) but reflects a
    completely different, unrelated conversation's size, not how full the
    main conversation's context window actually is. Defaults to "main" so a
    session persisted before this field existed still validates and behaves
    exactly as it did before (every entry was implicitly a main-turn entry
    back then)."""


class SessionCost(BaseModel):
    turns: list[TurnCost] = Field(default_factory=list)
    session_total_usd: float = 0.0
    total_tokens: int = 0


class PermissionGrant(BaseModel):
    tool_name: str
    argument_pattern: str | None = None
    scope: Literal["session", "always"] = "session"
    decision: Literal["allow", "deny"] = "allow"
    created_at: datetime = Field(default_factory=_utcnow)


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class Decision(BaseModel):
    """One entry in the session's decision log (see tools/builtin/decision_tool.py).
    Append-only audit trail — unlike TodoItem, an entry is never mutated once
    recorded; a change of mind is a new entry, not an edit of the old one."""

    decision: str
    rationale: str
    created_at: datetime = Field(default_factory=_utcnow)


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sess_"))
    schema_version: int = CURRENT_SCHEMA_VERSION
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    title: str | None = None
    model: str = ""
    gateway_base_url: str = ""
    messages: list[Message] = Field(default_factory=list)
    tool_invocations: list[ToolInvocation] = Field(default_factory=list)
    cost: SessionCost = Field(default_factory=SessionCost)
    permission_grants: list[PermissionGrant] = Field(default_factory=list)
    todos: list[TodoItem] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    working_dir: str | None = None
    """Where this session was created (Path.cwd() at the time) - resuming it
    from a different directory means every relative path in its tool-call
    history refers to a different set of files, so callers check this
    against the current cwd before resuming (see session/directory_check.py)
    and warn rather than silently proceeding. None for a session persisted
    before this field existed, or one that was never tied to a real cwd
    (e.g. some test fixtures) - never treated as a mismatch."""

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def derive_title(self) -> str:
        if self.title:
            return self.title
        for message in self.messages:
            if message.role == "user" and message.content:
                text = message.content.strip().splitlines()[0]
                return text[:60] + ("..." if len(text) > 60 else "")
        return "New session"


class SessionIndexEntry(BaseModel):
    id: str
    title: str
    updated_at: datetime
    model: str
    message_count: int
    total_cost_usd: float
