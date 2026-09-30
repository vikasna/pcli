"""pip_install: install Python packages via `python -m pip install`, given
either package names/specifiers, a requirements-style file, or both."""

from __future__ import annotations

import sys

from pcli.sandbox.base import ExecRequest
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_MAX_OUTPUT_CHARS = 100_000
_DEFAULT_TIMEOUT_S = 120.0


async def _pip_install(arguments: dict, ctx: ToolContext) -> ToolResult:
    packages = arguments.get("packages") or []
    requirements_file = arguments.get("requirements_file")
    if not packages and not requirements_file:
        return ToolResult(
            output="Nothing to install: provide 'packages' (a list of package names/specifiers) "
            "and/or 'requirements_file' (a path to a requirements-style file).",
            is_error=True,
        )

    # sys.executable is pcli's own host interpreter, which only exists on the
    # filesystem run_shell-style commands land in when the sandbox is the
    # subprocess backend (same machine, no containment). Under the Docker
    # backend that path doesn't resolve inside the container at all - fall
    # back to whatever "python" the image itself provides, same as any other
    # command sent through ctx.sandbox for that backend.
    python_bin = sys.executable if isinstance(ctx.sandbox, RestrictedSubprocessSandbox) else "python"
    argv = [python_bin, "-m", "pip", "install"]
    if requirements_file:
        argv += ["-r", requirements_file]
    argv += list(packages)

    requested_timeout_s = float(arguments.get("timeout_s", _DEFAULT_TIMEOUT_S))
    max_timeout_s = float(ctx.guardrails.max_shell_timeout_s)
    timeout_s = min(requested_timeout_s, max_timeout_s)

    # Installing packages inherently needs network access - under the Docker
    # backend, ExecRequest.network=False (the default everywhere else in this
    # package) maps to --network none, which would make every install fail.
    result = await ctx.sandbox.execute(
        ExecRequest(command=argv, cwd=ctx.cwd, timeout_s=timeout_s, network=True)
    )

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n[...output truncated...]"
    if requested_timeout_s > max_timeout_s:
        output += (
            f"\n[pcli] Requested timeout_s={requested_timeout_s:g} was clamped to the "
            f"guardrail limit of {max_timeout_s:g}s."
        )

    is_error = result.exit_code != 0 or result.timed_out
    if result.timed_out:
        output += (
            f"\n[pcli] Suggestion: pip install timed out at timeout_s={timeout_s:g}s — retry "
            "with a higher timeout_s (up to the guardrail ceiling) if it just needs more time "
            "to download/build."
        )

    summary = f"[exit_code={result.exit_code}, backend={result.backend_used}]\n{output}"
    return ToolResult(output=summary, is_error=is_error)


PIP_INSTALL = ToolSpec(
    name="pip_install",
    description="Install Python package(s) via 'python -m pip install' (never a bare 'pip', "
    "which can silently target the wrong interpreter). Give 'packages' (names/specifiers like "
    "'requests' or 'requests==2.31.0'), 'requirements_file' (a path to a requirements.txt-style "
    "file, passed as pip's -r), or both.",
    parameters={
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Package names or specifiers to install, e.g. "
                "['requests', 'numpy>=1.26'].",
            },
            "requirements_file": {
                "type": "string",
                "description": "Path to a requirements-style file (one package per line). "
                "Relative paths resolve against the working directory.",
            },
            "timeout_s": {
                "type": "number",
                "description": f"Timeout in seconds (default {_DEFAULT_TIMEOUT_S:g}). Raise this "
                "for large/slow installs, up to the guardrail ceiling (guardrails.toml's "
                "limits.max_shell_timeout_s).",
            },
        },
    },
    handler=_pip_install,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Installs Python package(s) via pip.",
    guardrail_path_arg="requirements_file",
    read_only=False,
)
