"""The one builtin tool that actually goes through the Sandbox."""

from __future__ import annotations

from pcli.sandbox.base import ExecRequest
from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_MAX_OUTPUT_CHARS = 100_000


async def _run_shell(arguments: dict, ctx: ToolContext) -> ToolResult:
    command = arguments["command"]
    timeout_s = float(arguments.get("timeout_s", 30))
    request = ExecRequest(command=command, cwd=ctx.cwd, timeout_s=timeout_s)
    result = await ctx.sandbox.execute(request)

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n[...output truncated...]"

    summary = f"[exit_code={result.exit_code}, backend={result.backend_used}]\n{output}"
    is_error = result.exit_code != 0 or result.timed_out
    return ToolResult(output=summary, is_error=is_error)


RUN_SHELL = ToolSpec(
    name="run_shell",
    description="Run a shell command in a sandboxed working directory. Use for builds, "
    "tests, git, package managers, etc.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout_s": {"type": "number", "description": "Timeout in seconds (default 30)."},
        },
        "required": ["command"],
    },
    handler=_run_shell,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Executes an arbitrary shell command.",
    guardrail_command_arg="command",
)
