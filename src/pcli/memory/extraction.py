"""Autonomous memory extraction: after a compaction pass archives a chunk of
old conversation away (agent/compaction.py's maybe_compact), review that
transcript - plus what's already known - and classify anything worth
noting into one of remember's two scopes (tools/builtin/memory_tool.py) -
global (persisted, cross-session, cross-project) or local (not persisted
at all - a correctly-labeled dead end for project-specific content, not a
lesser fallback). Runs as a small, tool-only AgentLoop scoped to just that
one tool - no sandbox, shell, or file access needed or wanted here, unlike
a real subagent (tools/_nested_agent.py).

The explicit local category exists because leaving this as an implicit
binary "durable enough to remember, or not" judgment demonstrably failed in
practice: a real derived entry once got filed under the global 'preference'
category even though its content (a specific package's import error in one
project's test script) was clearly project-specific, not durable. Giving
the model a concrete, named place for "worth noting but not global" - the
same kind of discrete choice it already makes among the four global
categories - is the fix: classifying into a parallel category is a more
reliable task for an LLM than making an abstract counterfactual judgment
("would this matter in some hypothetical future project?") inline while
also deciding whether to act at all.
"""

from __future__ import annotations

from dataclasses import replace

from pcli.agent.loop import AgentLoop
from pcli.cost.tracker import cost_budget_reason
from pcli.llm.models import ChatMessage, Usage, UsageEvent
from pcli.memory.models import render_memory_section
from pcli.memory.store import read_memory
from pcli.tools.base import ToolContext
from pcli.tools.builtin.memory_tool import REMEMBER
from pcli.tools.registry import ToolRegistry

_EXTRACTION_SYSTEM_PROMPT = (
    "You review a chunk of an in-progress coding-agent conversation and classify anything "
    "worth noting into exactly one of two scopes. Be strict - most reviews should add zero "
    "or one fact, not several.\n\n"
    "GLOBAL - call remember(content, category) with category='profile' (their role or "
    "domain), 'preference' (a recurring technical/workflow choice), 'style' (how they like "
    "responses/conversation), or 'common_ask' (a task they repeatedly ask for across "
    "different projects) ONLY for a fact that is true of the USER as a person, independent "
    "of whatever project this conversation happens to be about. Before using one of these "
    "four categories, check: would this sentence still make complete sense, with no "
    "confusion, read cold at the start of a totally unrelated project with none of this "
    "conversation's context? If no, it is not global - do not file it under one of these "
    "four categories just because it seems important right now.\n\n"
    "LOCAL - call remember(content, category='local') for anything that seems worth noting "
    "but is specific to the current project, repo, file, or task: project/package names, "
    "the specifics of a bug under investigation, a decision that only matters for this "
    "codebase. This is the correct, intentional choice for most of what comes up in a "
    "normal conversation - not a lesser fallback. Nothing saved this way is ever persisted "
    "or shown again; it exists so you have somewhere correct to put project-specific "
    "content instead of discarding it silently or stretching it to fit a global category.\n\n"
    "If genuinely nothing in this chunk is worth noting even locally, make no calls at all."
)

_MAX_EXTRACTION_ITERATIONS = 5


async def extract_memory(transcript: str, ctx: ToolContext) -> list[Usage]:
    """Reviews `transcript` (typically the plain-text rendering of the turns
    a compaction pass just archived - see chat.py's _run_compaction) via a
    tool-only AgentLoop scoped to REMEMBER, and returns the LLM usage
    incurred so the caller can fold it into cost tracking. Never raises for
    a "found nothing worth remembering" outcome - only for a real gateway
    failure, which the caller is expected to catch (mirrors maybe_compact's
    own GatewayError-can-propagate contract; ctx.gateway_client must already
    be set, same precondition _run_compaction already checks before calling
    maybe_compact in the first place)."""
    assert ctx.gateway_client is not None

    memory_registry = ToolRegistry()
    memory_registry.register(REMEMBER)
    extraction_ctx = replace(ctx, tool_registry=memory_registry)

    known = render_memory_section(read_memory().entries) or "(nothing remembered yet)"
    sub_loop = AgentLoop(
        ctx.gateway_client,
        model=ctx.model,
        tool_registry=memory_registry,
        permission_manager=ctx.permission_manager,
        tool_context_factory=lambda: extraction_ctx,
        max_tool_iterations=_MAX_EXTRACTION_ITERATIONS,
        max_response_tokens=ctx.max_response_tokens,
        temperature=ctx.temperature,
    )
    messages = [
        ChatMessage(
            role="system", content=f"{_EXTRACTION_SYSTEM_PROMPT}\n\nAlready known:\n{known}"
        ),
        ChatMessage(role="user", content=transcript),
    ]

    def _budget_check() -> str | None:
        if ctx.session is None:
            return None
        return cost_budget_reason(ctx.session, ctx.max_session_cost_usd)

    usages: list[Usage] = []
    async for event in sub_loop.run_turn(messages, budget_check=_budget_check):
        if isinstance(event, UsageEvent):
            usages.append(event.usage)
    return usages
