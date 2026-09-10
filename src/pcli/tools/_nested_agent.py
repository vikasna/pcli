"""Shared plumbing for tools that run a nested AgentLoop (spawn_subagent,
make_agent_tool): builds the restricted sub-registry/context, drives the
loop, and collects its outcome. Not a ToolSpec itself - each caller wraps
the result in its own ToolResult with tool-specific wording, and is
responsible for catching exceptions raised while draining the loop (so it
can report a tool-specific failure message).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from pcli.agent.loop import AgentLoop, ToolStartEvent, TurnCompleteEvent
from pcli.cost.context import ContextLimitTable, ContextUsage
from pcli.llm.models import ChatMessage, Usage, UsageEvent
from pcli.tools.base import ToolContext, ToolSpec

# A subagent's own conversation has no compaction of its own (unlike the
# main loop's auto-compaction) - this is purely informational, appended to
# a subagent's result so the parent/user has a real explanation on hand if
# the work looks incomplete, rather than just a bare iteration-cap note or
# an opaque gateway error. Deliberately higher than auto_compact_threshold's
# default of 0.8: that number triggers an actual summarization pass with
# headroom to spare, this one is just "worth mentioning", so it can sit
# closer to the ceiling before it's worth the noise.
HIGH_CONTEXT_USAGE_FRACTION = 0.85


@dataclass
class NestedAgentResult:
    final_text: str
    tool_call_count: int
    terminated_early: bool
    usages: list[Usage] = field(default_factory=list)
    context_usage: ContextUsage | None = None
    """How much of the model's context window the subagent's own
    conversation was using by the end of its run (based on the last LLM
    call's reported usage.total_tokens, same basis cost/context.py's
    current_context_usage uses for the main loop) - None if no call
    completed or the model has no known context limit."""


async def run_nested_agent(
    ctx: ToolContext,
    *,
    system_prompt: str,
    task: str,
    activity_label: str,
    allowed: Callable[[ToolSpec], bool],
    max_iterations: int,
) -> NestedAgentResult:
    assert ctx.gateway_client is not None
    assert ctx.tool_registry is not None

    sub_registry = ctx.tool_registry.filtered(allowed)
    child_ctx = replace(ctx, tool_registry=sub_registry, subagent_depth=ctx.subagent_depth + 1)
    sub_loop = AgentLoop(
        ctx.gateway_client,
        model=ctx.model,
        tool_registry=sub_registry,
        permission_manager=ctx.permission_manager,
        tool_context_factory=lambda: child_ctx,
        max_tool_iterations=max_iterations,
        # Inherit the parent's current dynamic max_tokens cap and sampling
        # temperature - without this the subagent silently ran uncapped/at-
        # default regardless of /max-response-tokens or /temperature, since
        # a fresh AgentLoop defaults both to None.
        max_response_tokens=ctx.max_response_tokens,
        temperature=ctx.temperature,
    )

    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task),
    ]

    final_text_parts: list[str] = []
    tool_call_count = 0
    terminated_early = False
    usages: list[Usage] = []
    if ctx.activity is not None:
        ctx.activity.start_subagent(activity_label)
    try:
        async for event in sub_loop.run_turn(messages, ask=ctx.ask):
            if isinstance(event, UsageEvent):
                usages.append(event.usage)
            elif isinstance(event, ToolStartEvent):
                if ctx.activity is not None:
                    ctx.activity.record_subagent_tool_call(
                        event.tool_call.function.name, event.tool_call.function.arguments
                    )
            elif isinstance(event, TurnCompleteEvent):
                terminated_early = event.terminated_early
                for message in event.new_messages:
                    if message.role != "assistant":
                        continue
                    if message.tool_calls:
                        tool_call_count += len(message.tool_calls)
                    if message.content:
                        final_text_parts.append(message.content)
    finally:
        if ctx.activity is not None:
            ctx.activity.finish_subagent()

    context_usage = None
    if usages and ctx.model:
        limit_tokens = ContextLimitTable.load().lookup(ctx.model)
        context_usage = ContextUsage(used_tokens=usages[-1].total_tokens, limit_tokens=limit_tokens)

    return NestedAgentResult(
        final_text="\n\n".join(part for part in final_text_parts if part).strip(),
        tool_call_count=tool_call_count,
        terminated_early=terminated_early,
        usages=usages,
        context_usage=context_usage,
    )


def context_usage_note(result: NestedAgentResult) -> str | None:
    """A note to append to a subagent's reported summary when its own
    conversation ended up using a large fraction of the model's context
    window - None if usage was unremarkable or unknown. Shared wording
    since, unlike the DID NOT FINISH message, there's nothing tool-specific
    to say here."""
    usage = result.context_usage
    if usage is None or usage.fraction < HIGH_CONTEXT_USAGE_FRACTION:
        return None
    return (
        f"[pcli] Note: this subagent's own conversation reached {usage.fraction:.0%} of its "
        f"~{usage.limit_tokens:,}-token context limit by the end of its run. Its own task history "
        "has no automatic compaction (unlike the main conversation) - if the result above looks "
        "incomplete or cut off, context size may be the real cause even if it wasn't reported as "
        "an iteration-limit failure. Consider splitting the task into smaller, narrower subagent "
        "calls rather than just raising iteration limits."
    )
