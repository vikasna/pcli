"""No-op sandbox backend: runs commands directly, with none of
RestrictedSubprocessSandbox's containment (no cwd jail, no env scrubbing, no
resource limits) or Docker's isolation.

Selected via sandbox_backend = "none" — an explicit, documented opt-out for
users who already run pcli somewhere they trust fully (e.g. inside their own
disposable container/VM), never the default. Guardrails and the permission
system still gate tool calls as usual; this only removes the *execution*
containment layer underneath them.
"""

from __future__ import annotations

import asyncio
import os

from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities


def _truncate(data: bytes, max_bytes: int) -> str:
    if len(data) <= max_bytes:
        return data.decode(errors="replace")
    return data[:max_bytes].decode(errors="replace") + "\n[...output truncated...]"


class NullSandbox(Sandbox):
    name = "none"

    def __init__(self, *, max_output_bytes: int = 2_000_000) -> None:
        self._max_output_bytes = max_output_bytes

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supports_network_isolation=False,
            supports_memory_limit=False,
            supports_cpu_limit=False,
        )

    async def execute(self, request: ExecRequest) -> ExecResult:
        # Full inherited environment (unlike RestrictedSubprocessSandbox's
        # allowlist scrub) plus any extra vars the caller asked for layered
        # on top — "no sandbox" means no restriction, not no environment.
        env = {**os.environ, **request.env}

        if isinstance(request.command, str):
            proc = await asyncio.create_subprocess_shell(
                request.command,
                cwd=str(request.cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE
                if request.stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *request.command,
                cwd=str(request.cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE
                if request.stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        timed_out = False
        stdin_bytes = request.stdin.encode() if request.stdin is not None else None
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(stdin_bytes), timeout=request.timeout_s
            )
        except TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                pass
            stdout_bytes, stderr_bytes = b"", b"[pcli] command timed out and was killed"

        return ExecResult(
            stdout=_truncate(stdout_bytes, self._max_output_bytes),
            stderr=_truncate(stderr_bytes, self._max_output_bytes),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
            backend_used=self.name,
        )
