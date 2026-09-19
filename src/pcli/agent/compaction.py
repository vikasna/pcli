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


def turn_boundaries(messages: list[Message]) -> list[int]:
    """Indices of role=='user' messages — the only safe places to cut, since
    a full turn's tool_calls/tool-result pairs always live entirely between
    one user message and the next. Cutting anywhere else risks splitting an
    assistant(tool_calls=...) from its matching tool-role response, which
    breaks the chat-completions wire format.

    Used (via compaction_cutoff) by both this module and
    agent/context_pruning.py — the same turn-boundary-safety reasoning
    applies to pruning old tool results, not just full-turn compaction."""
    return [i for i, m in enumerate(messages) if m.role == "user"]


def _round_boundaries(messages: list[Message]) -> list[int]:
    """Finer-grained fallback for turn_boundaries: every index safe to cut
    at (message.role != "tool", so a tool result is never separated from the
    assistant tool_calls message that requested it) - not just role=='user'.

    A single long-running task (one user message, then dozens of internal
    assistant/tool round trips as the model works through it) has exactly
    one 'user' boundary for its entire life. Gating on turn_boundaries alone
    means that session's eligibility check (len(boundaries) <= keep_recent_
    turns) never becomes false, no matter how much context that one turn's
    own history has accumulated - real debugged case: a session hit its
    model's context limit and stalled with no visible error (see
    cost/context.py's looks_like_context_ceiling), and /compact reported
    "nothing to compact" even though the conversation was enormous, because
    it had only ever received a single user message. Used only as a
    fallback, when turn_boundaries alone doesn't yield enough boundaries to
    safely keep keep_recent_turns of them - a normal multi-turn conversation
    is unaffected and keeps using turn_boundaries exactly as before."""
    return [i for i, m in enumerate(messages) if m.role != "tool"]


_MIN_ROUNDS_FOR_FALLBACK = 20
"""How many round trips a single (or few) user turn must accumulate before
the _round_boundaries fallback below kicks in at all. Deliberately much
bigger than any ordinary keep_recent_turns value (2 by default): an
ordinary short turn - a handful of tool calls, still the single most recent
thing the user is looking at - has nowhere near this many round-boundary
entries, so it's left completely alone, exactly as if the fallback didn't
exist. Only once a single turn has genuinely ballooned well past that (a
long autonomous task making dozens of tool calls) does the fallback treat
it as something worth compacting/pruning into."""


def compaction_cutoff(messages: list[Message], *, keep_recent_turns: int) -> int | None:
    """The index up to which messages are eligible to be summarized/pruned,
    keeping the most recent turns' worth of exchanges verbatim - None if
    there isn't enough history yet to safely do anything.

    Prefers turn_boundaries (real user-typed turns): if there are more of
    those than keep_recent_turns, behavior is exactly what it always was.
    Otherwise falls back to the finer _round_boundaries - but only once the
    single (or few) turn(s) have grown past _MIN_ROUNDS_FOR_FALLBACK, and
    even then keeping at least that many of the most recent rounds verbatim
    (not the much smaller keep_recent_turns itself, which is calibrated for
    whole turns, not individual round trips) - see both docstrings for why."""
    boundaries = turn_boundaries(messages)
    if len(boundaries) > keep_recent_turns:
        return boundaries[-keep_recent_turns] if keep_recent_turns > 0 else len(messages)

    round_boundaries = _round_boundaries(messages)
    fallback_keep = max(keep_recent_turns, _MIN_ROUNDS_FOR_FALLBACK)
    if len(round_boundaries) <= fallback_keep:
        return None
    return round_boundaries[-fallback_keep]


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
    keep_recent_turns+1 user turns, or - the fallback compaction_cutoff
    applies for a session dominated by one long, tool-call-heavy turn -
    round trips)."""
    cut_index = compaction_cutoff(session.messages, keep_recent_turns=keep_recent_turns)
    if cut_index is None:
        return None

    prefix_len = _system_prompt_prefix_len(session.messages)
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
