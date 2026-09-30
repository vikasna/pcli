"""record_decision: lets the LLM log a consequential decision it made,
with its reasoning, to the session's append-only decision log. Unlike
write_todos (which replaces the whole list every call), each call here
*adds* one entry — a decision doesn't get mutated once made; a change of
mind is a new entry, not an edit of the old one. This builds a persistent,
exportable audit trail (Session.decisions) that survives resuming, export,
and import, same as everything else on the session."""

from __future__ import annotations

from pcli.session.models import Decision
from pcli.tools.base import ToolContext, ToolResult, ToolSpec


def render_decisions(decisions: list[Decision]) -> str:
    """Real Markdown ("- " bullets, not "• ") - the one caller (the
    "resuming with N recorded decision(s)" system message in chat.py)
    renders this through rich.markdown.Markdown, which needs actual
    list-item syntax or it collapses multiple "\\n"-joined lines into one
    run-on paragraph."""
    if not decisions:
        return "No decisions recorded."
    return "\n".join(f"- {d.decision}" for d in decisions)


async def _record_decision(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.session is None:
        return ToolResult(output="No session available to record decisions in.", is_error=True)

    decision = arguments.get("decision")
    rationale = arguments.get("rationale")
    if not decision or not rationale:
        return ToolResult(
            output="Both 'decision' and 'rationale' are required.\n"
            "[pcli] Suggestion: decision is what was decided, stated plainly; rationale is "
            "why, including supporting evidence when there is any.",
            is_error=True,
        )

    entry = Decision(decision=decision, rationale=rationale)
    ctx.session.decisions.append(entry)
    return ToolResult(output=f"Decision recorded: {decision}")


RECORD_DECISION = ToolSpec(
    name="record_decision",
    description="Log a consequential decision you made during this task, with your reasoning — "
    "e.g. choosing one approach over another, a non-obvious tradeoff, or anything the user "
    "might later want to understand 'why' about. Builds a persistent, exportable audit trail "
    "for the session (each call adds one entry; it doesn't replace prior ones — a change of "
    "mind is a new entry). Use it for consequential decisions, not routine tool calls — most "
    "actions don't need this. Include the evidence behind the decision in the rationale when "
    "there is any (a file:line, a command's output, a search result).",
    parameters={
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "description": "What was decided, stated plainly.",
            },
            "rationale": {
                "type": "string",
                "description": "Why — the reasoning behind it, including supporting evidence "
                "when there is any.",
            },
        },
        "required": ["decision", "rationale"],
    },
    handler=_record_decision,
    needs_permission=False,
    plan_mode_safe=True,
    read_only=False,
)
