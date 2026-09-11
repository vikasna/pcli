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

from pcli.tools._nested_agent import context_usage_note, run_nested_agent
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.builtin.todo_tool import WRITE_TODOS

SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"

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
            "(no gateway/tools/permissions configured).\n"
            "[pcli] Suggestion: handle this task directly with the tools you already have "
            "instead of delegating it.",
            is_error=True,
        )

    task = arguments["task"]
    allowed_tool_names = arguments.get("allowed_tools")
    requested_max_iterations = arguments.get("max_iterations")
    # Subagents keep this structural safety cap even in local-api mode
    # (ctx.max_tool_iterations may be None there, meaning "unlimited" for the
    # parent) — nesting depth/iteration count is a distinct concern from
    # turn/cost limiting, see this module's own docstring.
    if requested_max_iterations:
        max_iterations = min(int(requested_max_iterations), ctx.subagent_max_iterations)
    else:
        max_iterations = ctx.subagent_max_iterations

    def _allowed(tool: ToolSpec) -> bool:
        if tool.name == SPAWN_SUBAGENT_TOOL_NAME:
            return False  # subagents can never spawn further subagents
        if (
            allowed_tool_names is not None
            and tool.name not in allowed_tool_names
            and tool.name != WRITE_TODOS.name
        ):
            # write_todos is always let through regardless of what the calling
            # model's allowed_tools argument specifies (same kind of
            # structural guarantee as the spawn_subagent exclusion above) -
            # the "use write_todos for multi-step work" instruction
            # (_TODO_DISCIPLINE, tools/_nested_agent.py) would otherwise only
            # ever apply when the calling model happens to remember to
            # include it, which a small/unreliable model can't be counted on
            # to do consistently. Harmless even when the model never touches
            # it. Always plan_mode_safe, so this never bypasses plan mode.
            return False
        return not (ctx.plan_mode and not tool.plan_mode_safe)

    try:
        result = await run_nested_agent(
            ctx,
            system_prompt=_SUBAGENT_SYSTEM_PROMPT,
            task=task,
            activity_label=task,
            allowed=_allowed,
            max_iterations=max_iterations,
        )
    except Exception as exc:  # noqa: BLE001 - surface subagent failure, don't crash the parent turn
        return ToolResult(
            output=f"Subagent failed: {exc}\n"
            "[pcli] Suggestion: retry with a narrower, more specific task description, or "
            "handle it directly yourself instead of delegating.",
            is_error=True,
        )

    result_text = result.final_text or "(subagent produced no final text output)"
    summary = f"[subagent made {result.tool_call_count} tool call(s)]\n{result_text}"
    if result.terminated_early:
        summary = (
            "[pcli] SUBAGENT DID NOT FINISH — it hit its tool-call iteration limit "
            f"({max_iterations}) before completing the task below. Treat this as INCOMPLETE: "
            "do not report the task as done, and verify what (if anything) was actually "
            "produced (e.g. list_dir/read_file the expected output) before telling the user it "
            "succeeded. If the task genuinely needs more tool calls, either raise "
            "subagent_max_iterations via PCLI_SUBAGENT_MAX_ITERATIONS/config.toml, or split the "
            "work into a narrower follow-up task.\n\n" + summary
        )
    note = context_usage_note(result)
    if note:
        summary += "\n\n" + note
    return ToolResult(output=summary, is_error=result.terminated_early, extra_usage=result.usages)


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
                "description": "Optional cap on the subagent's own tool-call iterations. There "
                "is always a hard built-in ceiling regardless of this value (configurable via "
                "subagent_max_iterations) - if a task genuinely needs many tool calls (running "
                "several scripts, iterating on errors, generating a report), request a "
                "generous number here rather than assuming the default is enough.",
            },
        },
        "required": ["task"],
    },
    handler=_spawn_subagent,
    needs_permission=True,
    risk_description="Spawns a subagent that can call tools (including sandboxed ones) on its own.",
    plan_mode_safe=True,
)
