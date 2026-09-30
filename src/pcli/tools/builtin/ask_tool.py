"""ask_user_question: lets the model pause a turn to ask the user something
directly — a genuine ambiguity it can't safely resolve itself, or a choice
between approaches only they can make. Mirrors opencode's "ask" tool.
"""

from __future__ import annotations

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"


async def _ask_user_question(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.ask_question is None:
        return ToolResult(
            output="No UI available to ask the user a question in this context.\n"
            "[pcli] Suggestion: state your assumption plainly instead and proceed with it, "
            "rather than blocking on an answer you can't get here.",
            is_error=True,
        )

    question = arguments["question"]
    options = arguments.get("options")

    # When this fires from inside a subagent, the user otherwise sees a bare
    # question with zero context - the subagent's own intermediate work never
    # enters the main conversation (see tools/builtin/subagent_tool.py), so
    # there's nothing else to relate the question to. Prepend a short summary
    # of what the subagent has been doing so far, directly in the question
    # text itself (rather than a separate UI channel), so it reaches whatever
    # implements ask_question - the TUI modal today, anything else later.
    activity = ctx.activity
    sub = activity.subagent if activity is not None else None
    asked_question = question
    if sub is not None and activity is not None:
        activity.set_subagent_pending_question(question, options)
        recent = ", ".join(c.name for c in sub.call_log[-5:]) or "none yet"
        asked_question = (
            f"[This question is from a subagent working on: \"{sub.task}\" — "
            f"{len(sub.call_log)} tool call(s) so far, most recent: {recent}. "
            "Use /subagent for the full detail.]\n\n" + question
        )

    try:
        answer = await ctx.ask_question(asked_question, options)
    finally:
        if sub is not None and activity is not None:
            activity.clear_subagent_pending_question()
    return ToolResult(output=answer)


ASK_USER_QUESTION = ToolSpec(
    name=ASK_USER_QUESTION_TOOL_NAME,
    description="Ask the user a direct question and wait for their answer — for a genuine "
    "ambiguity you can't safely resolve yourself, or a choice between approaches only they can "
    "make. Optionally provide a short list of suggested answers as 'options'; the user can "
    "still type a free-text answer regardless of whether options are given. Don't reach for "
    "this for something you could reasonably infer, verify yourself, or state as an assumption "
    "and proceed — see the system prompt's guidance on when asking is actually warranted.",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask the user."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: a short list of suggested answers, shown as quick "
                "choices. The user can still type something else.",
            },
        },
        "required": ["question"],
    },
    handler=_ask_user_question,
    needs_permission=False,
    plan_mode_safe=True,
    read_only=True,
)
