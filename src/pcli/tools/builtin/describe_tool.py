"""describe_tool: lets the model ask for more detail about any tool in its
current tool set - the full description, parameter schema, permission
requirements, and a handful of representative example calls with a
one-line explanation each.

Motivation: a tool's one-line entry in the `tools=[...]` payload sent to
the model is sometimes not enough to use it correctly on the first try -
edit_file's three mutually-exclusive modes (see fs_tools.py) are the
concrete case that prompted this: without a clear example, some models
guessed at how to insert text rather than asking. describe_tool gives the
model somewhere cheap to ask first, instead of guessing and getting a
permission-gated call wrong (or silently no-op'ing, as edit_file's old
old_string==new_string footgun used to).

Examples are hand-curated (_EXAMPLES below), not derived from the JSON
schema - a schema alone shows shape, not intent, and doesn't distinguish
"pick exactly one of these optional parameter groups" (edit_file) from
"only path is ever required, everything else is optional" (list_dir).
"""

from __future__ import annotations

import json
from typing import NamedTuple

from pcli.tools.base import ToolContext, ToolResult, ToolSpec


class ToolExample(NamedTuple):
    arguments: dict
    explanation: str


# Tool name -> a handful of representative calls. Deliberately not
# exhaustive for every tool (some are self-explanatory from their
# description/schema alone - a plain "read_file(path)" needs no example) -
# curated where a tool has more than one mode, an easy-to-miss parameter,
# or a real reported point of confusion.
_EXAMPLES: dict[str, list[ToolExample]] = {
    "read_file": [
        ToolExample({"path": "src/app.py"}, "Read a file's full contents."),
    ],
    "write_file": [
        ToolExample(
            {"path": "notes.txt", "content": "first line\nsecond line\n"},
            "Create a new file, or overwrite an existing one entirely.",
        ),
    ],
    "edit_file": [
        ToolExample(
            {
                "path": "src/app.py",
                "old_string": "def total(a, b):\n    return a - b",
                "new_string": "def total(a, b):\n    return a + b",
            },
            "Replace mode: old_string is an exact, unique, verbatim copy of existing "
            "content; swap it for new_string.",
        ),
        ToolExample(
            {
                "path": "src/app.py",
                "old_string": "\"user\"",
                "new_string": "\"account\"",
                "replace_all": True,
            },
            "Replace mode with replace_all: replace every occurrence at once (e.g. "
            "renaming something throughout the file), instead of requiring uniqueness.",
        ),
        ToolExample(
            {"path": "src/app.py", "insert_after_line": 12, "new_string": "import os"},
            "Insert mode: add a new line after line 12 without touching anything else - "
            "the correct way to insert text (never set new_string equal to old_string to "
            "fake this in replace mode).",
        ),
        ToolExample(
            {"path": "src/app.py", "insert_after_line": 0, "new_string": "#!/usr/bin/env python3"},
            "Insert mode with insert_after_line=0: insert at the very start of the file, "
            "before the first line.",
        ),
        ToolExample(
            {"path": "src/app.py", "delete_start_line": 40, "delete_end_line": 45},
            "Delete mode: remove lines 40 through 45 inclusive. Re-check line numbers with "
            "read_file/grep -n first - they shift after every edit to the same file.",
        ),
    ],
    "list_dir": [
        ToolExample({"path": "src/pcli/tools"}, "List a directory's immediate contents."),
        ToolExample({}, "List the working directory itself (path defaults to '.')."),
    ],
    "glob_search": [
        ToolExample(
            {"pattern": "**/*.py", "path": "src"},
            "Find every .py file anywhere under src/, recursively.",
        ),
    ],
    "grep": [
        ToolExample(
            {"pattern": r"def \w+_tool\(", "path": "src/pcli/tools", "glob": "**/*.py"},
            "Search for a regex pattern across text files under a directory.",
        ),
    ],
    "diff_files": [
        ToolExample(
            {"path_a": "config.old.toml", "path_b": "config.toml"},
            "Show a unified diff between two existing files - read-only, doesn't change "
            "either one.",
        ),
    ],
    "apply_patch": [
        ToolExample(
            {
                "path": "src/app.py",
                "patch": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n line one\n-old line\n+new line\n line three\n",
            },
            "Apply a unified diff (e.g. one diff_files just produced) in one call - requires "
            "an exact match against the file's current content, no fuzzy offsets.",
        ),
    ],
    "run_shell": [
        ToolExample({"command": "pytest tests/ -q"}, "Run a command and get its output/exit code back immediately."),
        ToolExample(
            {"command": "git status", "timeout_s": 30},
            "Override the default timeout for a command expected to take a while.",
        ),
    ],
    "run_shell_background": [
        ToolExample(
            {"command": "python -m http.server 8000"},
            "Start a long-running/server-like command that shouldn't block the turn - "
            "returns a job_id immediately instead of waiting for it to exit.",
        ),
    ],
    "read_background_output": [
        ToolExample(
            {"job_id": "job_abc123"},
            "Check on a background job's output so far without stopping it.",
        ),
    ],
    "stop_background_process": [
        ToolExample({"job_id": "job_abc123"}, "Kill a background job started by run_shell_background."),
    ],
    "download_file": [
        ToolExample(
            {"url": "https://example.test/dataset.csv", "path": "data/dataset.csv"},
            "Save a URL's content to disk - for binary/large files, not for reading text "
            "(use web_fetch for that instead).",
        ),
    ],
    "web_fetch": [
        ToolExample(
            {"url": "https://example.test/docs/api"},
            "Fetch a URL and get back its readable text (HTML converted to plain text).",
        ),
    ],
    "web_search": [
        ToolExample(
            {"query": "python asyncio cancel task"},
            "Short, keyword-based query (2-6 words) - not a full question or sentence.",
        ),
    ],
    "pip_install": [
        ToolExample({"packages": ["requests", "pydantic>=2"]}, "Install one or more packages by name/specifier."),
        ToolExample(
            {"requirements_file": "requirements.txt"},
            "Install everything listed in a requirements-style file instead.",
        ),
    ],
    "search_python": [
        ToolExample({"query": "parse a URL"}, "Find installed Python modules/functions relevant to a task, by description."),
    ],
    "inspect_python_module": [
        ToolExample(
            {"module": "httpx", "query": "how to set a timeout"},
            "See a specific installed module's actual signatures/docstrings, once you know its name.",
        ),
    ],
    "call_python": [
        ToolExample(
            {"qualified_name": "json.dumps", "kwargs": {"obj": {"a": 1}, "indent": 2}},
            "Call an installed Python function directly and get its return value back, "
            "without writing/running a throwaway script.",
        ),
    ],
    "write_todos": [
        ToolExample(
            {
                "todos": [
                    {"content": "Read the failing test", "status": "completed"},
                    {"content": "Fix the off-by-one bug", "status": "in_progress"},
                    {"content": "Re-run the test suite", "status": "pending"},
                ]
            },
            "Submit the FULL current list every time (this replaces it, it doesn't append) "
            "- exactly one item may be 'in_progress' at a time.",
        ),
    ],
    "record_decision": [
        ToolExample(
            {
                "decision": "Use httpx instead of requests",
                "rationale": "the codebase is already async elsewhere; requests has no native async support",
            },
            "Log a consequential decision and why - builds a persistent, exportable audit "
            "trail, separate from the todo list.",
        ),
    ],
    "remember": [
        ToolExample(
            {"content": "Prefers terse responses with no trailing summary", "category": "preference"},
            "Save something durable about the user to global, cross-session memory - not "
            "for task-scoped facts that only matter to the current session.",
        ),
    ],
    "ask_user_question": [
        ToolExample(
            {"question": "Should I use PostgreSQL or SQLite for this?", "options": ["PostgreSQL", "SQLite"]},
            "Pause and ask the user directly when a decision genuinely can't be made without "
            "them - not a first resort for anything you could reasonably infer or verify.",
        ),
    ],
    "fetch_artifact": [
        ToolExample(
            {"artifact_id": "art_001a0bf445376b27a5fa718"},
            "Retrieve a large tool result or compacted transcript that was archived out of "
            "the live conversation (see its own \"archived as artifact_id='...'\" note).",
        ),
        ToolExample(
            {"artifact_id": "art_001a0bf445376b27a5fa718", "pattern": "TODO", "context_lines": 2},
            "Search within a large archived artifact instead of pulling in the whole thing.",
        ),
    ],
    "ask_artifact": [
        ToolExample(
            {"artifact_id": "art_001a0bf445376b27a5fa718", "question": "What was the final error message?"},
            "Local-api mode only: ask a question about a large artifact and get a direct "
            "answer back (an extra LLM call, free on a local gateway) instead of reading the "
            "raw content yourself.",
        ),
    ],
    "spawn_subagent": [
        ToolExample(
            {"task": "Find every place JWT tokens are validated in this codebase and summarize the logic"},
            "Delegate a self-contained, well-scoped investigation to a nested agent - its "
            "intermediate tool calls stay out of the main conversation, only its final "
            "answer comes back.",
        ),
    ],
    "explore_codebase": [
        ToolExample(
            {"query": "how is user authentication implemented?"},
            "A pre-configured subagent for read-only codebase investigation - faster to "
            "reach for than spawn_subagent when the task is exactly this shape.",
        ),
    ],
    "explore_files": [
        ToolExample({"query": "find all config files and summarize what each one controls"}, "A pre-configured subagent for broad file discovery/summarization."),
    ],
    "explore_logs": [
        ToolExample({"query": "find the root cause of the crash in the most recent log file"}, "A pre-configured subagent for digging through log output."),
    ],
    "verify_computation": [
        ToolExample({"query": "double-check this SQL migration is safe against a 50M-row table"}, "A pre-configured subagent for independently verifying a claim/calculation."),
    ],
    "deep_research": [
        ToolExample({"query": "compare the tradeoffs of gRPC vs REST for this service"}, "A pre-configured subagent for a broader, multi-source research task."),
    ],
    "data_analysis": [
        ToolExample({"query": "summarize trends in sales.csv"}, "A pre-configured subagent for analyzing data files."),
    ],
    "write_documentation": [
        ToolExample({"query": "write a README section explaining the new config option"}, "A pre-configured subagent for drafting documentation."),
    ],
    "register_agent_tool": [
        ToolExample(
            {
                "name": "changelog_writer",
                "description": "Drafts a changelog entry from a git diff.",
                "persona_prompt": "You write terse, user-facing changelog entries from a diff.",
                "allowed_tools": ["run_shell", "read_file"],
            },
            "Create a new, reusable, narrowly-scoped agent tool the model (or a future turn) "
            "can call by name, like the built-in explore_*/deep_research tools.",
        ),
    ],
    "register_toolbox_tool": [
        ToolExample(
            {"name": "kubectl"},
            "Discover an installed CLI on PATH and turn its subcommands into callable tools.",
        ),
        ToolExample(
            {"name": "my_script", "path": "scripts/my_script.py"},
            "Register a self-authored script directly, bypassing PATH lookup.",
        ),
    ],
    "browser_navigate": [
        ToolExample({"url": "https://example.test/dashboard"}, "Start/continue a browser session by navigating to a URL."),
    ],
    "browser_click": [
        ToolExample({"selector": "text=Sign in"}, "Click an element, identified by a Playwright locator (CSS, or text=... for visible text)."),
    ],
    "browser_type": [
        ToolExample({"selector": "#username", "text": "alice"}, "Type into a field, replacing whatever was already there."),
    ],
    "browser_press_key": [
        ToolExample({"key": "Enter"}, "Send a single keypress - e.g. to submit a form after browser_type."),
    ],
    "browser_wait_for": [
        ToolExample(
            {"selector": "#results", "timeout_s": 15},
            "Wait for an element to appear before continuing, instead of guessing how long "
            "to sleep after an action that loads content asynchronously.",
        ),
    ],
    "browser_read_page": [
        ToolExample({}, "Read the visible text of the currently open page - no arguments needed."),
    ],
    "browser_screenshot": [
        ToolExample({}, "Save a screenshot of the currently open page to disk and get back its path."),
    ],
}


def _format_examples(name: str) -> str:
    examples = _EXAMPLES.get(name)
    if not examples:
        return (
            "(No curated examples for this tool yet - its description and parameter "
            "schema above should be enough; it's a simple, single-purpose call.)"
        )
    lines = []
    for i, example in enumerate(examples, start=1):
        call = json.dumps(example.arguments, ensure_ascii=False)
        lines.append(f"{i}. {name}({call})\n   {example.explanation}")
    return "\n".join(lines)


async def _describe_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    name = arguments["name"]
    if ctx.tool_registry is None:
        return ToolResult(output="No tool registry available in this context.", is_error=True)
    tool = ctx.tool_registry.get(name)
    if tool is None:
        available = ", ".join(sorted(t.name for t in ctx.tool_registry))
        return ToolResult(
            output=f"No tool named '{name}' in your current tool set.\n"
            f"[pcli] Suggestion: check the exact spelling - available tools: {available}",
            is_error=True,
        )
    permission_line = f"Needs permission: {tool.needs_permission}"
    if tool.needs_permission and tool.risk_description:
        permission_line += f" — {tool.risk_description}"
    parts = [
        f"# {tool.name}",
        tool.description,
        "",
        permission_line,
        f"Available in plan mode: {tool.plan_mode_safe}",
        "",
        "## Parameters (JSON schema)",
        json.dumps(tool.parameters, indent=2, ensure_ascii=False),
        "",
        "## Example calls",
        _format_examples(name),
    ]
    return ToolResult(output="\n".join(parts))


DESCRIBE_TOOL = ToolSpec(
    name="describe_tool",
    description="Get full detail about any tool in your current tool set — its complete "
    "description, parameter schema, permission requirements, and a few representative "
    "example calls with a one-line explanation each. Use this before an unfamiliar or "
    "tricky call (e.g. one with several optional/mutually-exclusive parameters) instead of "
    "guessing at the right arguments from its one-line summary alone.",
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact name of the tool to describe, e.g. 'edit_file'.",
            }
        },
        "required": ["name"],
    },
    handler=_describe_tool,
    needs_permission=False,
    plan_mode_safe=True,
)
