"""Tool-result pruning: a lightweight, mechanical (no LLM call) pass that
shrinks the content of old, already-resolved tool-role messages down to a
short placeholder — archiving the original first (retrievable via
fetch_artifact, same ArtifactStore mechanism agent/loop.py's
_archive_if_large and agent/compaction.py's maybe_compact already use).

Distinct from maybe_compact: compaction is coarse and late (only fires once
context usage crosses a threshold, replaces entire old turns with one LLM
summary). This pass runs every turn, touches only tool-role message content
(never user/assistant text or the assistant's own tool_calls), and needs no
model call — small, already-spent tool results (a `which foo` probe, a
one-line `ls`) are exactly what this targets, well before compaction's
threshold would ever notice them.
"""

from __future__ import annotations

import json

from pcli.agent.compaction import turn_boundaries
from pcli.session.models import Message, Session
from pcli.tools.artifacts import ArtifactStore


def extract_purpose(arguments_json: str) -> str | None:
    """Best-effort: given a tool call's raw JSON arguments string, returns
    the model-supplied "purpose" value (see ToolSpec.to_openai_tool) if
    present and well-formed, else None. Never raises — a malformed or
    missing purpose is just not shown, not an error."""
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    purpose = arguments.get("purpose")
    return purpose if isinstance(purpose, str) and purpose.strip() else None


def _find_purpose(messages: list[Message], tool_call_id: str | None) -> str | None:
    """Scans every assistant tool_calls entry for one matching tool_call_id
    and extracts its purpose — tool_call ids are unique per call, so a full
    scan (not just the immediately preceding message) is correct and, for
    real session sizes, cheap."""
    if tool_call_id is None:
        return None
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if call.id == tool_call_id:
                return extract_purpose(call.function.arguments)
    return None


def prune_old_tool_results(
    session: Session, *, keep_recent_turns: int, artifact_store: ArtifactStore
) -> int:
    """Replaces old tool-role message content with a compact placeholder,
    mutating session.messages in place — the most recent keep_recent_turns
    turns' tool results stay verbatim. Idempotent: a message already pruned
    (pruned_artifact_id set) is skipped, so this is safe to call every turn
    without re-archiving or duplicating artifacts. Returns how many messages
    were pruned this call (0 if nothing was old enough yet)."""
    boundaries = turn_boundaries(session.messages)
    if len(boundaries) <= keep_recent_turns:
        return 0  # nothing old enough to prune yet

    cutoff_index = boundaries[-keep_recent_turns] if keep_recent_turns > 0 else len(session.messages)

    pruned_count = 0
    for i in range(cutoff_index):
        message = session.messages[i]
        if message.role != "tool" or message.pruned_artifact_id is not None or not message.content:
            continue

        original_content = message.content
        artifact_id = artifact_store.put(original_content)
        purpose = _find_purpose(session.messages, message.tool_call_id)

        note = f"[Pruned tool result ({len(original_content):,} chars) to save context."
        if purpose:
            note += f" Purpose: {purpose}."
        note += f" Call fetch_artifact(artifact_id='{artifact_id}') if you need it.]"

        message.content = note
        message.pruned_artifact_id = artifact_id
        pruned_count += 1

    return pruned_count
