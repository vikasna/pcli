"""remember: lets the LLM persist a fact, in one of two scopes it must pick
explicitly (see REMEMBER's own description below for the exact instructions
given for telling them apart). The global categories (profile/preference/
style/common_ask) write to the global, cross-session memory store (memory/
store.py) - unlike record_decision (decision_tool.py), which logs to the
current session only, this is meant to still be true, and useful, in a
completely different session and project. category='local' is the
deliberate escape hatch for everything else: project/task-specific content
that should not survive past this conversation - _remember below intercepts
it and never calls add_entry, so nothing is written anywhere. Two callers:
the main agent, immediately whenever the user explicitly asks to be
remembered; and memory/extraction.py's own small tool-only agent, which
calls it after reviewing a chunk of conversation for anything durable
worth keeping (and, now, anything merely project-specific worth noting as
local instead of silently dropping it or miscategorizing it as global)."""

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

    if category == "local":
        # Deliberately not persisted anywhere - see MemoryCategory's own
        # docstring. This category exists so the model has a correct place
        # to put something project/task-specific instead of discarding it
        # silently or miscategorizing it as one of the global ones below
        # (confirmed in practice: a real derived entry once got filed under
        # "preference" even though it was specific to one project's test
        # script - exactly the failure this branch exists to avoid).
        return ToolResult(output=f"Noted (local to this session, not saved globally): {content}")

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
    description="Persist a fact to pcli's memory. Two categorically different scopes, pick "
    "carefully:\n\n"
    "GLOBAL (profile/preference/style/common_ask) - saved to disk, injected into the system "
    "prompt of every future session, in every project, forever (until forgotten). Use these "
    "ONLY for something that is true of the USER as a person, independent of whatever you "
    "happen to be working on right now - it must still make complete sense if read cold in a "
    "totally unrelated project with no shared context. Test before calling: would this "
    "sentence confuse someone working on a different codebase next week? If yes, it is not "
    "global.\n\n"
    "LOCAL - NOT saved anywhere, not visible again after this turn. Use this for anything "
    "that is specific to the current project, repo, file, bug, or task - project/package/"
    "file names, the specifics of a bug you're mid-investigation on, a decision that only "
    "matters for this codebase. This is the correct, intentional choice for most of what "
    "comes up in a normal session - it is not a lesser option.\n\n"
    "Use GLOBAL immediately whenever the user explicitly asks to be remembered (\"remember "
    "that I use tabs\", \"don't suggest X again\") - that is always about the user, not the "
    "project. When in doubt between the two, choose LOCAL: the cost of wrongly going global "
    "is a stale, confusing fact bleeding into unrelated future work; the cost of wrongly "
    "going local is just not remembering something that turns out to have been durable.",
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
                "description": "GLOBAL scope - profile = nature of work/role, preference = a "
                "recurring technical/workflow choice, style = how they like responses/"
                "conversation, common_ask = a recurring task pattern. LOCAL scope - local = "
                "specific to the current project/task only; never persisted, never resurfaces.",
            },
            "source": {
                "type": "string",
                "enum": ["explicit", "derived"],
                "description": "'explicit' if the user directly asked you to remember this "
                "(protects it from automatic eviction later); omit or use 'derived' otherwise. "
                "Meaningless for category='local', which is never persisted regardless.",
            },
        },
        "required": ["content", "category"],
    },
    handler=_remember,
    needs_permission=False,
    plan_mode_safe=True,
    read_only=False,
)
