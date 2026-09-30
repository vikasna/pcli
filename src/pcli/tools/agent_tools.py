"""make_agent_tool: builds a ToolSpec around a fixed persona + fixed
allowed-tool set, calling into a fresh nested AgentLoop exactly the way
spawn_subagent does — the difference is the persona/allowed-tools are baked
in at registration time instead of supplied by the calling model per-call.

Used both for the three built-in exploration agent tools registered in
build_default_registry() and for user/model-defined ones persisted via
register_agent_tool (tools/builtin/agent_tool_register_tool.py).
"""

from __future__ import annotations

from pcli.tools._nested_agent import context_usage_note, run_nested_agent
from pcli.tools.base import ToolContext, ToolResult, ToolSpec


def make_agent_tool(
    name: str,
    description: str,
    persona_prompt: str,
    allowed_tool_names: list[str],
    *,
    plan_mode_safe: bool = False,
    read_only: bool = False,
) -> ToolSpec:
    async def _handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        if ctx.gateway_client is None or ctx.tool_registry is None or ctx.permission_manager is None:
            return ToolResult(
                output=f"'{name}' isn't available in this context "
                "(no gateway/tools/permissions configured).\n"
                "[pcli] Suggestion: handle this task directly with the tools you already have "
                "instead of delegating it.",
                is_error=True,
            )

        query = arguments["query"]

        def _allowed(tool: ToolSpec) -> bool:
            if tool.name not in allowed_tool_names:
                return False
            # Same plan-mode inheritance as spawn_subagent: a nested agent
            # tool can't be used as a bypass while the parent is restricted.
            return not (ctx.plan_mode and not tool.plan_mode_safe)

        try:
            result = await run_nested_agent(
                ctx,
                system_prompt=persona_prompt,
                task=query,
                activity_label=f"{name}: {query}",
                allowed=_allowed,
                max_iterations=ctx.subagent_max_iterations,
            )
        except Exception as exc:  # noqa: BLE001 - surface failure, don't crash the parent turn
            return ToolResult(
                output=f"'{name}' failed: {exc}\n"
                "[pcli] Suggestion: retry with a narrower, more specific query, or handle it "
                "directly yourself instead of delegating.",
                is_error=True,
            )

        result_text = result.final_text or f"({name} produced no final text output)"
        summary = f"[{name} made {result.tool_call_count} tool call(s)]\n{result_text}"
        if result.terminated_early:
            summary = (
                f"[pcli] '{name}' DID NOT FINISH — it hit its tool-call iteration limit "
                f"({ctx.subagent_max_iterations}) before completing. Treat this as INCOMPLETE: "
                "do not report the task as done without verifying what was actually produced.\n\n"
                + summary
            )
        note = context_usage_note(result)
        if note:
            summary += "\n\n" + note
        return ToolResult(
            output=summary, is_error=result.terminated_early, extra_usage=result.usages
        )

    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A clear, self-contained description of what to look into "
                    "and what to report back.",
                }
            },
            "required": ["query"],
        },
        handler=_handler,
        needs_permission=True,
        risk_description=f"Runs a nested agent ({name}) that can call tools within its "
        "fixed allowed set on its own.",
        plan_mode_safe=plan_mode_safe,
        read_only=read_only,
    )


_EXPLORE_CODEBASE_PERSONA = (
    "You are a code exploration subagent spawned by another AI agent (pcli) to answer a "
    "specific question about a codebase. Use the tools available to you to investigate, then "
    "give a clear, self-contained final answer — reference file_path:line where it helps. The "
    "parent agent only sees your final text, not your intermediate steps."
)

_EXPLORE_FILES_PERSONA = (
    "You are a file/directory exploration subagent spawned by another AI agent (pcli) to "
    "answer a specific question about the layout or contents of files/directories. Use the "
    "tools available to you to investigate, then give a clear, self-contained final answer. "
    "The parent agent only sees your final text, not your intermediate steps."
)

_EXPLORE_LOGS_PERSONA = (
    "You are a log exploration subagent spawned by another AI agent (pcli) to answer a "
    "specific question about the contents of on-disk log files. Use the tools available to "
    "you to investigate, then give a clear, self-contained final answer — reference "
    "file_path:line where it helps. The parent agent only sees your final text, not your "
    "intermediate steps."
)

EXPLORE_CODEBASE = make_agent_tool(
    name="explore_codebase",
    description="Delegate a focused code-exploration question (e.g. 'how is auth implemented', "
    "'where is X defined') to a subagent restricted to read-only code search/inspection tools. "
    "Use this to isolate exploratory work without cluttering the main conversation.",
    persona_prompt=_EXPLORE_CODEBASE_PERSONA,
    allowed_tool_names=[
        "read_file",
        "list_dir",
        "glob_search",
        "grep",
        "search_python",
        "inspect_python_module",
    ],
    plan_mode_safe=True,
    read_only=True,
)

EXPLORE_FILES = make_agent_tool(
    name="explore_files",
    description="Delegate a focused question about file/directory layout or contents (e.g. "
    "'find the config files', 'what's in this directory tree') to a subagent restricted to "
    "read-only filesystem tools.",
    persona_prompt=_EXPLORE_FILES_PERSONA,
    allowed_tool_names=["list_dir", "glob_search", "read_file"],
    plan_mode_safe=True,
    read_only=True,
)

EXPLORE_LOGS = make_agent_tool(
    name="explore_logs",
    description="Delegate a focused question about on-disk log file contents (e.g. 'find the "
    "first error in this log', 'summarize what happened around timestamp X') to a subagent "
    "restricted to read_file/grep — deliberately excludes run_shell so it stays read-only.",
    persona_prompt=_EXPLORE_LOGS_PERSONA,
    allowed_tool_names=["read_file", "grep"],
    plan_mode_safe=True,
    read_only=True,
)

_WRITE_DOCUMENTATION_PERSONA = (
    "You are a documentation subagent spawned by another AI agent (pcli) to write or update "
    "documentation for a code change. Before writing a single line, read the existing "
    "documentation in this area (open a couple of neighboring doc files) and read the actual "
    "code being documented — never draft from a description alone. Match the existing "
    "documentation's structure, tone, and level of detail; you are extending an existing "
    "document, not authoring a new one in your own style. Verify claims — function signatures, "
    "flags, file paths, parameter names, defaults — against the real code rather than "
    "paraphrasing what you were told changed. Give a clear, self-contained final answer naming "
    "exactly which file(s) you created or edited. The parent agent only sees your final text, "
    "not your intermediate steps."
)

_VERIFY_COMPUTATION_PERSONA = (
    "You are a computation-verification subagent spawned by another AI agent (pcli) to check a "
    "calculation, algorithm, or numeric claim. Do not trust mental arithmetic or reasoning-in-"
    "text as a final answer — write and run code to compute or check every numeric claim "
    "yourself. If you catch yourself asserting a number without having actually run code to "
    "produce it, stop and go compute it instead. State your verdict plainly (the claim is "
    "correct, incorrect, or partially correct), showing the value you computed next to the "
    "claimed value and explaining any discrepancy. The parent agent only sees your final text, "
    "not your intermediate steps."
)

_DEEP_RESEARCH_PERSONA = (
    "You are a research subagent spawned by another AI agent (pcli) to investigate a question "
    "thoroughly, using both local sources (files, logs, code, shell/git history) and the web "
    "(web_search, web_fetch). Start with short, broad queries to see what's available, then "
    "progressively narrow your focus rather than committing to one angle immediately. Scale your "
    "effort to the question's complexity — simple fact-finding needs only a few tool calls; a "
    "genuinely open question may need many more. Never state a fact found in only one place as "
    "settled — note it as single-sourced, or find a second, independent source; if sources "
    "disagree, say so explicitly rather than silently picking one. Cite the actual URLs or file "
    "paths you drew conclusions from, never a citation reconstructed from memory. Give a clear, "
    "structured final report. The parent agent only sees your final text, not your intermediate "
    "steps."
)

_DATA_ANALYSIS_PERSONA = (
    "You are a data-analysis subagent spawned by another AI agent (pcli) to explore or analyze a "
    "local dataset. Before characterizing the data in any way, load it and inspect it "
    "programmatically — shape, dtypes, null rates, and real computed aggregates, not a guess "
    "from glancing at a few rows. State any assumption you had to make and any data-quality "
    "issue you notice (nulls, outliers, duplicates, inconsistent types, unexpected cardinality) "
    "as a matter of course, even if not specifically asked. Give a clear, self-contained final "
    "answer backed by the numbers you actually computed. The parent agent only sees your final "
    "text, not your intermediate steps."
)

WRITE_DOCUMENTATION = make_agent_tool(
    name="write_documentation",
    description="Delegate writing or updating documentation for a code change to a subagent "
    "that reads the existing docs' style and the actual code before writing, so the result "
    "matches this project's conventions instead of a generic template.",
    persona_prompt=_WRITE_DOCUMENTATION_PERSONA,
    allowed_tool_names=["read_file", "list_dir", "glob_search", "grep", "write_file", "edit_file"],
    plan_mode_safe=False,
    read_only=False,
)

VERIFY_COMPUTATION = make_agent_tool(
    name="verify_computation",
    description="Delegate checking a calculation, algorithm, or numeric claim to a subagent "
    "that verifies it by writing and running code rather than reasoning about it in text — "
    "catches arithmetic/logic errors a purely verbal check would miss.",
    persona_prompt=_VERIFY_COMPUTATION_PERSONA,
    allowed_tool_names=["read_file", "write_file", "call_python", "run_shell"],
    plan_mode_safe=False,
    read_only=False,
)

DEEP_RESEARCH = make_agent_tool(
    name="deep_research",
    description="Delegate a question that needs thorough investigation — local (files, logs, "
    "code, git history) and/or web (web_search, web_fetch) — to a subagent that cross-checks "
    "claims against multiple sources and cites them, rather than a single-source shallow answer.",
    persona_prompt=_DEEP_RESEARCH_PERSONA,
    allowed_tool_names=[
        "read_file",
        "list_dir",
        "glob_search",
        "grep",
        "search_python",
        "inspect_python_module",
        "run_shell",
        "web_search",
        "web_fetch",
    ],
    plan_mode_safe=True,
    read_only=False,
)

DATA_ANALYSIS = make_agent_tool(
    name="data_analysis",
    description="Delegate exploring or analyzing a local dataset (CSV/JSON/etc.) to a subagent "
    "that computes real statistics via code rather than guessing at what's in the data, and "
    "flags data-quality issues as a matter of course.",
    persona_prompt=_DATA_ANALYSIS_PERSONA,
    allowed_tool_names=["read_file", "list_dir", "glob_search", "call_python", "run_shell", "write_file"],
    plan_mode_safe=False,
    read_only=False,
)
