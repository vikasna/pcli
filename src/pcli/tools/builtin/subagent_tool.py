"""spawn_subagent: lets the LLM delegate a focused sub-task to a fresh,
independent agent loop that shares the parent's gateway/sandbox/permissions.

Subagents can never spawn further subagents — this tool is always filtered
out of the tool registry a subagent runs with, so nesting is capped at one
level by construction, not by a runtime counter that could be bypassed.

Only the subagent's final answer and tool-call count are reported back to
the parent; its own intermediate tool calls are not individually recorded
in the parent session (they still go through the same permission/guardrail
gates, just aren't logged as top-level ToolInvocations). Its LLM usage IS
folded back into cost tracking via ToolResult.extra_usage, since it's real
spend the session total must reflect.
"""

from __future__ import annotations

from dataclasses import replace

from pcli.agent.loop import AgentLoop, ToolStartEvent, TurnCompleteEvent
from pcli.llm.models import ChatMessage, Usage, UsageEvent
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.registry import ToolRegistry

SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"

_DEFAULT_MAX_ITERATIONS = 15

_SUBAGENT_SYSTEM_PROMPT = (
    "You are a subagent spawned by another AI agent (pcli) to handle one focused task. "
    "Use the tools available to you to complete it, then give a clear, self-contained final "
    "answer — the parent agent only sees your final text, not your intermediate steps. You "
    "have no memory of the parent conversation beyond the task description you were given."
)


async def _spawn_subagent(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.gateway_client is None or ctx.tool_registry is None or ctx.permission_manager is None:
        return ToolResult(
            output="Subagents aren't available in this context "
            "(no gateway/tools/permissions configured).",
            is_error=True,
        )

    task = arguments["task"]
    allowed_tool_names = arguments.get("allowed_tools")
    requested_max_iterations = arguments.get("max_iterations")
    # Subagents keep this structural safety cap even in local-api mode
    # (ctx.max_tool_iterations may be None there, meaning "unlimited" for the
    # parent) — nesting depth/iteration count is a distinct concern from
    # turn/cost limiting, see spawn_subagent's module docstring.
    if requested_max_iterations:
        max_iterations = min(int(requested_max_iterations), _DEFAULT_MAX_ITERATIONS)
    elif ctx.max_tool_iterations is None:
        max_iterations = _DEFAULT_MAX_ITERATIONS
    else:
        max_iterations = min(ctx.max_tool_iterations, _DEFAULT_MAX_ITERATIONS)

    sub_registry = ToolRegistry()
    for tool in ctx.tool_registry:
        if tool.name == SPAWN_SUBAGENT_TOOL_NAME:
            continue  # subagents can never spawn further subagents
        if allowed_tool_names is not None and tool.name not in allowed_tool_names:
            continue
        sub_registry.register(tool)

    child_ctx = replace(ctx, tool_registry=sub_registry, subagent_depth=ctx.subagent_depth + 1)
    sub_loop = AgentLoop(
        ctx.gateway_client,
        model=ctx.model,
        tool_registry=sub_registry,
        permission_manager=ctx.permission_manager,
        tool_context_factory=lambda: child_ctx,
        max_tool_iterations=max_iterations,
    )

    messages = [
        ChatMessage(role="system", content=_SUBAGENT_SYSTEM_PROMPT),
        ChatMessage(role="user", content=task),
    ]

    final_text_parts: list[str] = []
    tool_call_count = 0
    usages: list[Usage] = []
    if ctx.activity is not None:
        ctx.activity.start_subagent(task)
    try:
        async for event in sub_loop.run_turn(messages, ask=ctx.ask):
            if isinstance(event, UsageEvent):
                usages.append(event.usage)
            elif isinstance(event, ToolStartEvent):
                if ctx.activity is not None:
                    ctx.activity.record_subagent_tool_call(event.tool_call.function.name)
            elif isinstance(event, TurnCompleteEvent):
                for message in event.new_messages:
                    if message.role != "assistant":
                        continue
                    if message.tool_calls:
                        tool_call_count += len(message.tool_calls)
                    if message.content:
                        final_text_parts.append(message.content)
    except Exception as exc:  # noqa: BLE001 - surface subagent failure, don't crash the parent turn
        return ToolResult(output=f"Subagent failed: {exc}", is_error=True, extra_usage=usages)
    finally:
        if ctx.activity is not None:
            ctx.activity.finish_subagent()

    result_text = "\n\n".join(part for part in final_text_parts if part).strip()
    if not result_text:
        result_text = "(subagent produced no final text output)"
    summary = f"[subagent made {tool_call_count} tool call(s)]\n{result_text}"
    return ToolResult(output=summary, extra_usage=usages)


SPAWN_SUBAGENT = ToolSpec(
    name=SPAWN_SUBAGENT_TOOL_NAME,
    description="Delegate a focused sub-task to a fresh subagent, which runs its own "
    "independent tool-calling loop and reports back a final answer. Use this to isolate "
    "exploratory or multi-step work (e.g. 'research how X is implemented in this repo') "
    "without cluttering the main conversation with intermediate tool calls. The subagent "
    "has no memory of this conversation — give it a fully self-contained task description.",
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "A clear, self-contained description of what the subagent should "
                "do and what it should report back.",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: restrict the subagent to only these tool names. Omit "
                "to give it the same tools available to you (it can never spawn further subagents).",
            },
            "max_iterations": {
                "type": "integer",
                "description": "Optional cap on the subagent's own tool-call iterations "
                "(default: a conservative built-in limit).",
            },
        },
        "required": ["task"],
    },
    handler=_spawn_subagent,
    needs_permission=True,
    risk_description="Spawns a subagent that can call tools (including sandboxed ones) on its own.",
)
