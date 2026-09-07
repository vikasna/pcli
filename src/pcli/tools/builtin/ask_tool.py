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
    answer = await ctx.ask_question(question, options)
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
)
