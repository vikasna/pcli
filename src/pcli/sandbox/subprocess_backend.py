"""Cross-platform fallback sandbox: process + filesystem containment via a
cwd jail, a scrubbed environment, resource limits where the OS supports them,
and a wall-clock timeout as the universal safety net.

This backend does NOT provide network isolation on any platform — that
requires Docker. Guardrails should treat network-sensitive tool calls as
always-ask when this backend is active.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from pcli.sandbox.base import (
    ExecRequest,
    ExecResult,
    Sandbox,
    SandboxCapabilities,
    SandboxSecurityError,
)
from pcli.sandbox.limits import is_posix, kill_process_tree, make_posix_preexec_fn

_ENV_ALLOW_NAMES = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "PATHEXT",
    "COMSPEC",
    "PYTHONIOENCODING",
}
_ENV_DENY_SUBSTRINGS = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL", "_AUTH")


def _scrub_env(extra: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {
        name: value for name, value in os.environ.items() if name.upper() in _ENV_ALLOW_NAMES
    }
    for key, value in extra.items():
        if any(pattern in key.upper() for pattern in _ENV_DENY_SUBSTRINGS):
            continue
        env[key] = value
    return env


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _truncate(data: bytes, max_bytes: int) -> str:
    if len(data) <= max_bytes:
        return data.decode(errors="replace")
    return data[:max_bytes].decode(errors="replace") + "\n[...output truncated...]"


class RestrictedSubprocessSandbox(Sandbox):
    name = "subprocess"

    def __init__(
        self,
        *,
        allowed_roots: list[Path] | None = None,
        max_output_bytes: int = 2_000_000,
        cpu_seconds: int | None = 30,
        memory_bytes: int | None = 512 * 1024 * 1024,
    ) -> None:
        self._allowed_roots = [p.expanduser().resolve() for p in (allowed_roots or [Path.cwd()])]
        self._max_output_bytes = max_output_bytes
        self._cpu_seconds = cpu_seconds
        self._memory_bytes = memory_bytes

    def capabilities(self) -> SandboxCapabilities:
        posix = is_posix()
        return SandboxCapabilities(
            supports_network_isolation=False,
            supports_memory_limit=posix,
            supports_cpu_limit=posix,
        )

    def _validate_cwd(self, cwd: Path) -> Path:
        resolved = cwd.expanduser().resolve()
        for root in self._allowed_roots:
            if resolved == root or _is_relative_to(resolved, root):
                return resolved
        raise SandboxSecurityError(f"cwd '{resolved}' is outside all allowed roots")

    async def execute(self, request: ExecRequest) -> ExecResult:
        cwd = self._validate_cwd(request.cwd)
        env = _scrub_env(request.env)

        popen_kwargs: dict = {}
        if is_posix():
            popen_kwargs["preexec_fn"] = make_posix_preexec_fn(
                cpu_seconds=self._cpu_seconds, memory_bytes=self._memory_bytes
            )
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        if isinstance(request.command, str):
            proc = await asyncio.create_subprocess_shell(
                request.command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE if request.stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *request.command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE if request.stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )

        timed_out = False
        stdin_bytes = request.stdin.encode() if request.stdin is not None else None
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(stdin_bytes), timeout=request.timeout_s
            )
        except TimeoutError:
            timed_out = True
            kill_process_tree(proc.pid)
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
