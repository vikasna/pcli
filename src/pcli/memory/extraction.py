"""Autonomous memory extraction: after a compaction pass archives a chunk of
old conversation away (agent/compaction.py's maybe_compact), review that
transcript - plus what's already known - for anything durable and
cross-session-worthy, and persist it via the same remember tool the main
agent uses for explicit requests (tools/builtin/memory_tool.py). Runs as a
small, tool-only AgentLoop scoped to just that one tool - no sandbox, shell,
or file access needed or wanted here, unlike a real subagent
(tools/_nested_agent.py).
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
    "You review a chunk of an in-progress coding-agent conversation and decide what, if "
    "anything, is worth remembering about the user for FUTURE sessions - not this one. Call "
    "remember(content, category) for each durable, cross-session fact you find: their role or "
    "domain (category='profile'), a recurring technical/workflow preference "
    "(category='preference'), how they like responses/conversation (category='style'), or a "
    "task they repeatedly ask for (category='common_ask'). Do NOT call it for anything specific "
    "to only this task - file names, one-off requests, details that won't matter in a different "
    "project. If nothing in this chunk is durable and cross-session-worthy, make no calls at "
    "all - most reviews should add zero or one fact, not several."
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
