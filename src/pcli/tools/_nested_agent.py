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
from pcli.agent.prompt import environment_section
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

# Appended to every nested agent's own system prompt (spawn_subagent's
# generic persona, and every make_agent_tool persona in agent_tools.py) -
# none of them otherwise inherit BASE_SYSTEM_PROMPT's "Verifying your own
# work"/"Tracking work" discipline, since a subagent's system prompt fully
# replaces the main loop's rather than extending it (same reason
# environment_section() below is prepended separately - a subagent
# previously got no OS/shell/path facts at all and independently defaulted
# to the same Linux/bash training-data bias the main loop's own environment
# section exists to prevent). Confirmed as a real gap from an observed
# session: a subagent made a single tool call, never used write_todos
# despite having it available, and reported a large multi-file assignment
# as fully complete - the parent then relayed that fabricated summary to
# the user with no verification of its own. This is a single injection
# point (every nested-agent tool funnels through run_nested_agent) rather
# than duplicating this text across each persona string.
_CORE_DISCIPLINE = (
    "Your final answer must describe only actions you actually took via tool calls in this run — "
    "never describe a step as done because it's what a typical solution would include; if you "
    "didn't execute it, it isn't done. Before writing that final answer, verify any concrete "
    "deliverable you're about to claim (a file, a report, a script, a downloaded dataset) actually "
    "exists and has the content you're about to describe — e.g. list_dir/read_file it — rather "
    "than asserting it from the task description alone."
)

# Appended separately, and only when write_todos is actually in this
# subagent's own tool registry - it's a fixed, curated allowed-tool list for
# every make_agent_tool persona (explore_codebase, write_documentation, ...)
# and none of them include write_todos, so telling them to use a tool they
# don't have would be actively wrong. Only spawn_subagent can ever have it
# (only when the calling model includes it in its own allowed_tools).
_TODO_DISCIPLINE = (
    " Before starting a task with more than a couple of steps, use write_todos to lay out a plan "
    "and keep it updated as you go — this is what keeps a multi-step task from collapsing into a "
    "single tool call that doesn't actually do the work."
)


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

    discipline = _CORE_DISCIPLINE
    if "write_todos" in sub_registry:
        discipline += _TODO_DISCIPLINE
    messages = [
        ChatMessage(
            role="system", content=f"{environment_section()}\n\n{system_prompt}\n\n{discipline}"
        ),
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
