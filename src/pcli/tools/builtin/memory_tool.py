"""remember: lets the LLM persist a fact about the user to the global,
cross-session memory store (memory/store.py) - unlike record_decision
(decision_tool.py), which logs to the current session only, this is global:
it's meant to still be true, and useful, in a completely different session
and project. Two callers: the main agent, immediately whenever the user
explicitly asks to be remembered; and memory/extraction.py's own small
tool-only agent, which calls it after reviewing a chunk of conversation for
anything durable worth keeping."""

from __future__ import annotations

from typing import cast, get_args

from pcli.memory.models import MemoryCategory, MemorySource
from pcli.memory.store import add_entry
from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_VALID_CATEGORIES = set(get_args(MemoryCategory))


async def _remember(arguments: dict, ctx: ToolContext) -> ToolResult:
    content = (arguments.get("content") or "").strip()
    category = arguments.get("category")
    if not content or category not in _VALID_CATEGORIES:
        return ToolResult(
            output=f"Both 'content' and a valid 'category' ({', '.join(sorted(_VALID_CATEGORIES))}) "
            "are required.",
            is_error=True,
        )

    source: MemorySource = "explicit" if arguments.get("source") == "explicit" else "derived"
    entry = add_entry(
        content,
        category=cast(MemoryCategory, category),
        source=source,
        max_entries=ctx.memory_max_entries,
    )
    return ToolResult(output=f"Remembered ({entry.category}): {entry.content}")


REMEMBER = ToolSpec(
    name="remember",
    description="Persist a fact about the user to pcli's global, cross-session memory - "
    "available in every future session (and to subagents), not just this one. Use it "
    "immediately whenever the user explicitly asks to be remembered (\"remember that I "
    "use tabs\", \"don't suggest X again\"). Only for durable, cross-session facts about "
    "the user (their role, a recurring preference, how they like responses, a task they "
    "often ask for) - never for details specific to the current task alone.",
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember, stated plainly in one or two sentences.",
            },
            "category": {
                "type": "string",
                "enum": sorted(_VALID_CATEGORIES),
                "description": "profile = nature of work/role, preference = a recurring "
                "technical/workflow choice, style = how they like responses/conversation, "
                "common_ask = a recurring task pattern.",
            },
            "source": {
                "type": "string",
                "enum": ["explicit", "derived"],
                "description": "'explicit' if the user directly asked you to remember this "
                "(protects it from automatic eviction later); omit or use 'derived' otherwise.",
            },
        },
        "required": ["content", "category"],
    },
    handler=_remember,
    needs_permission=False,
    plan_mode_safe=True,
    read_only=False,
)
