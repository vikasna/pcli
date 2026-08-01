"""Direct, unsandboxed shell passthrough for the user's own `!command` input.

Deliberately bypasses the sandbox/guardrails/permission-prompt machinery:
those exist to constrain the LLM, not the human sitting at the keyboard. A
user typing `!command` gets their real environment and cwd, exactly like a
normal terminal — and unlike LLM-invoked tools, nothing here ever touches
the session, the artifact library, or the LLM's context window. It's a pure
side-effecting escape hatch for inspecting things without leaving pcli.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pcli.sandbox.base import ExecResult

DEFAULT_TIMEOUT_S = 120.0
_MAX_OUTPUT_CHARS = 50_000


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...output truncated...]"


async def run_passthrough_command(
    command: str, *, cwd: Path, timeout_s: float = DEFAULT_TIMEOUT_S
) -> ExecResult:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            pass
        stdout_bytes, stderr_bytes = b"", b"[pcli] command timed out and was killed"

    return ExecResult(
        stdout=_truncate(stdout_bytes.decode(errors="replace"), _MAX_OUTPUT_CHARS),
        stderr=_truncate(stderr_bytes.decode(errors="replace"), _MAX_OUTPUT_CHARS),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        timed_out=timed_out,
        backend_used="passthrough",
    )
