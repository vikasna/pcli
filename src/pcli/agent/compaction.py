"""Auto-compaction: when a session's conversation grows large relative to its
model's context window, summarizes the oldest turns via a single dedicated
LLM call and replaces them with the summary — archiving the original, full
transcript to the artifact library first (the same ArtifactStore/
fetch_artifact mechanism that already archives oversized tool results in
agent/loop.py's _archive_if_large), so nothing is silently lost, just moved
out of the live context.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcli.llm.client import GatewayClient
from pcli.llm.models import ChatMessage, Usage
from pcli.session.models import Message, Session
from pcli.tools.artifacts import ArtifactStore

_COMPACTION_SYSTEM_PROMPT = (
    "You are summarizing an in-progress coding-agent conversation so it can continue with "
    "much less context. Write a concise but complete summary covering: what the user asked "
    "for, what has been done so far (files changed, commands run, key decisions), the "
    "current state of any todo list, and anything still pending or unresolved. Do not "
    "include pleasantries or restate this instruction — write plain prose/bullets the "
    "assistant can use to pick up exactly where it left off."
)


@dataclass
class CompactionResult:
    summary_message: Message
    artifact_id: str
    messages_compacted: int
    usage: Usage


def _turn_boundaries(messages: list[Message]) -> list[int]:
    """Indices of role=='user' messages — the only safe places to cut, since
    a full turn's tool_calls/tool-result pairs always live entirely between
    one user message and the next. Cutting anywhere else risks splitting an
    assistant(tool_calls=...) from its matching tool-role response, which
    breaks the chat-completions wire format."""
    return [i for i, m in enumerate(messages) if m.role == "user"]


def _system_prompt_prefix_len(messages: list[Message]) -> int:
    """1 if the very first message is the session's leading system prompt
    (role=='system'), else 0. Deliberately checks only messages[0], not a
    run of every leading system-role message: a previous compaction's own
    summary message also has role=='system' and sits right after the real
    prompt, but must stay eligible to be folded into a *later* compaction —
    counting a run would permanently protect it instead."""
    return 1 if messages and messages[0].role == "system" else 0


def _render_transcript(messages: list[Message]) -> str:
    """Plain-text rendering of a message range for archiving — a human/LLM
    -readable record retrievable via fetch_artifact, not sent to any model
    as-is."""
    lines: list[str] = []
    for m in messages:
        header = f"--- {m.role} ---"
        if m.tool_calls:
            calls = ", ".join(f"{c.function.name}({c.function.arguments})" for c in m.tool_calls)
            lines.append(f"{header}\n[tool_calls: {calls}]")
        elif m.content:
            lines.append(f"{header}\n{m.content}")
    return "\n\n".join(lines)


async def maybe_compact(
    session: Session,
    *,
    gateway_client: GatewayClient,
    model: str | None,
    artifact_store: ArtifactStore,
    keep_recent_turns: int = 2,
) -> CompactionResult | None:
    """Compacts the oldest turns of session.messages in place, returning
    None if there isn't enough history to safely compact yet (fewer than
    keep_recent_turns+1 user turns)."""
    boundaries = _turn_boundaries(session.messages)
    if len(boundaries) <= keep_recent_turns:
        return None

    prefix_len = _system_prompt_prefix_len(session.messages)
    cut_index = boundaries[-keep_recent_turns]
    to_compact = session.messages[prefix_len:cut_index]
    if not to_compact:
        return None

    transcript = _render_transcript(to_compact)
    artifact_id = artifact_store.put(transcript)

    summary_messages = [
        ChatMessage(role="system", content=_COMPACTION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=transcript),
    ]
    assistant_message, usage = await gateway_client.collect(summary_messages, model=model)
    summary_text = assistant_message.content or "(no summary produced)"

    note = (
        f"\n\n[Compacted {len(to_compact)} earlier message(s) to reduce context usage. "
        f"Archived as artifact_id='{artifact_id}'. Call fetch_artifact(artifact_id="
        f"'{artifact_id}') if you need something specific from the original conversation.]"
    )
    summary_message = Message(role="system", content=summary_text + note)

    session.messages[prefix_len:cut_index] = [summary_message]

    return CompactionResult(
        summary_message=summary_message,
        artifact_id=artifact_id,
        messages_compacted=len(to_compact),
        usage=usage,
    )
