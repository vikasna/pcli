"""The builtin tools that go through the Sandbox: run_shell (blocking, the
common case) and run_shell_background/read_background_output/
stop_background_process (for commands with no natural end, or long enough
that blocking the turn on them isn't worth it — see
sandbox/subprocess_backend.py's BackgroundJob)."""

from __future__ import annotations

from pcli.sandbox.base import ExecRequest, SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_MAX_OUTPUT_CHARS = 100_000
_BACKGROUND_UNSUPPORTED = (
    "Background execution is only supported by the 'subprocess' sandbox backend "
    "(not available with the current backend)."
)
_UNKNOWN_JOB_SUGGESTION = (
    "check the \"Started background job '...'\" message from when run_shell_background was "
    "called — job ids aren't recoverable any other way."
)


def _timeout_suggestion(timeout_s: float, max_timeout_s: float) -> str:
    if timeout_s < max_timeout_s:
        return (
            f"the command timed out at timeout_s={timeout_s:g}s. Try raising timeout_s (up to "
            f"the guardrail ceiling of {max_timeout_s:g}s) if it just needs more time, or use "
            "run_shell_background if it has no natural end (a dev server, a watcher) or you'd "
            "rather keep working while it runs."
        )
    return (
        f"the command timed out even at the guardrail ceiling of {max_timeout_s:g}s. Use "
        "run_shell_background instead — it starts the command without blocking the turn, and "
        "read_background_output lets you check on it."
    )


def _shell_failure_suggestion(output: str) -> str | None:
    """Pattern-matched against a small set of failure signatures confirmed
    from a real debugged session: the identical Windows "'pip' is not
    recognized" and "Python was not found" (python3-vs-python) errors each
    got hit twice in a row before the model self-corrected by trial and
    error, and a ModuleNotFoundError happened despite an earlier pip
    install having reported "already satisfied" - for a different Python
    interpreter than the one actually running the script."""
    lowered = output.lower()
    if "'pip' is not recognized" in lowered or "pip: command not found" in lowered:
        return "'pip' isn't directly on PATH here — use 'python -m pip' instead."
    if "python was not found" in lowered and "microsoft store" in lowered:
        return "this system's Python launcher is 'python', not 'python3' — retry with 'python'."
    if "modulenotfounderror" in lowered:
        return (
            'a prior "pip install" succeeding doesn\'t guarantee it targeted the same '
            'interpreter running this script — check which one is actually active: '
            'python -c "import sys; print(sys.executable)".'
        )
    return None


async def _run_shell(arguments: dict, ctx: ToolContext) -> ToolResult:
    command = arguments["command"]
    requested_timeout_s = float(arguments.get("timeout_s", 30))
    max_timeout_s = float(ctx.guardrails.max_shell_timeout_s)
    timeout_s = min(requested_timeout_s, max_timeout_s)
    request = ExecRequest(command=command, cwd=ctx.cwd, timeout_s=timeout_s)
    result = await ctx.sandbox.execute(request)

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n[...output truncated...]"
    if requested_timeout_s > max_timeout_s:
        output += (
            f"\n[pcli] Requested timeout_s={requested_timeout_s:g} was clamped to the "
            f"guardrail limit of {max_timeout_s:g}s. For longer-running commands, use "
            "run_shell_background instead."
        )

    is_error = result.exit_code != 0 or result.timed_out
    if result.timed_out:
        output += f"\n[pcli] Suggestion: {_timeout_suggestion(timeout_s, max_timeout_s)}"
    elif is_error:
        suggestion = _shell_failure_suggestion(output)
        if suggestion:
            output += f"\n[pcli] Suggestion: {suggestion}"

    summary = f"[exit_code={result.exit_code}, backend={result.backend_used}]\n{output}"
    return ToolResult(output=summary, is_error=is_error)


RUN_SHELL = ToolSpec(
    name="run_shell",
    description="Run a shell command in a sandboxed working directory and wait for it to "
    "finish. Use for builds, tests, git, package managers, etc. For commands with no natural "
    "end (dev servers, watchers) or that may run longer than a few minutes, use "
    "run_shell_background instead of a very large timeout_s.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout_s": {
                "type": "number",
                "description": "Timeout in seconds (default 30). Raise this for commands "
                "known to take longer, up to the guardrail ceiling (see guardrails.toml's "
                "limits.max_shell_timeout_s).",
            },
        },
        "required": ["command"],
    },
    handler=_run_shell,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Executes an arbitrary shell command.",
    guardrail_command_arg="command",
)


async def _run_shell_background(arguments: dict, ctx: ToolContext) -> ToolResult:
    if not isinstance(ctx.sandbox, RestrictedSubprocessSandbox):
        return ToolResult(output=_BACKGROUND_UNSUPPORTED, is_error=True)
    command = arguments["command"]
    try:
        job_id = await ctx.sandbox.start_background(
            command=command, cwd=ctx.cwd, max_jobs=ctx.guardrails.max_background_jobs
        )
    except SandboxSecurityError as exc:
        return ToolResult(output=str(exc), is_error=True)
    return ToolResult(
        output=f"Started background job '{job_id}': {command}\n"
        f"Use read_background_output(job_id='{job_id}') to check on it, and "
        f"stop_background_process(job_id='{job_id}') when you're done with it."
    )


RUN_SHELL_BACKGROUND = ToolSpec(
    name="run_shell_background",
    description="Starts a shell command in the background and returns immediately with a "
    "job_id, instead of blocking until it finishes like run_shell. Use for long-running "
    "commands (dev servers, watchers, long builds/installs/test suites) where you want to "
    "keep working and check on progress later via read_background_output. Only available "
    "when the sandbox backend is 'subprocess'.",
    parameters={
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The shell command to run."}},
        "required": ["command"],
    },
    handler=_run_shell_background,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Starts a shell command that keeps running in the background.",
    guardrail_command_arg="command",
)


async def _read_background_output(arguments: dict, ctx: ToolContext) -> ToolResult:
    if not isinstance(ctx.sandbox, RestrictedSubprocessSandbox):
        return ToolResult(output=_BACKGROUND_UNSUPPORTED, is_error=True)
    job_id = arguments["job_id"]
    max_chars = int(arguments.get("max_chars", 4000))
    reset = bool(arguments.get("reset", False))
    read = ctx.sandbox.read_background(job_id, max_chars=max_chars, reset=reset)
    if read is None:
        return ToolResult(
            output=f"No background job with id '{job_id}'.\n[pcli] Suggestion: {_UNKNOWN_JOB_SUGGESTION}",
            is_error=True,
        )
    job, new_stdout, new_stderr, has_more = read

    status = "running" if job.running else f"exited (exit_code={job.exit_code})"
    lines = [f"status: {status}"]
    if new_stdout:
        lines.append(f"--- new stdout ---\n{new_stdout}")
    if new_stderr:
        lines.append(f"--- new stderr ---\n{new_stderr}")
    if not new_stdout and not new_stderr:
        lines.append("(no new output since your last read)")
    if has_more:
        lines.append("[pcli] More output is available — call again to keep reading.")
    return ToolResult(output="\n".join(lines))


READ_BACKGROUND_OUTPUT = ToolSpec(
    name="read_background_output",
    description="Reads the stdout/stderr a background job (started via run_shell_background) "
    "has produced since your last read of it, and whether it's still running or has exited. "
    "Only returns new output each call — call again if the result says more is available.",
    parameters={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "max_chars": {
                "type": "integer",
                "description": "Cap on characters returned per stream this call (default 4000).",
            },
            "reset": {
                "type": "boolean",
                "description": "Re-read from the beginning instead of continuing where you left off.",
            },
        },
        "required": ["job_id"],
    },
    handler=_read_background_output,
    needs_permission=False,
    needs_sandbox=True,
)


async def _stop_background_process(arguments: dict, ctx: ToolContext) -> ToolResult:
    if not isinstance(ctx.sandbox, RestrictedSubprocessSandbox):
        return ToolResult(output=_BACKGROUND_UNSUPPORTED, is_error=True)
    job_id = arguments["job_id"]
    stopped = await ctx.sandbox.stop_background(job_id)
    if not stopped:
        return ToolResult(
            output=f"No background job with id '{job_id}'.\n[pcli] Suggestion: {_UNKNOWN_JOB_SUGGESTION}",
            is_error=True,
        )
    return ToolResult(output=f"Stopped background job '{job_id}'.")


STOP_BACKGROUND_PROCESS = ToolSpec(
    name="stop_background_process",
    description="Kills a background process started via run_shell_background.",
    parameters={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    handler=_stop_background_process,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Kills a running background process.",
)
