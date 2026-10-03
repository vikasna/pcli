"""Chooses a Sandbox backend once at startup: Docker if reachable, else the
restricted-subprocess fallback. The chosen backend is surfaced in the status
bar so the user always knows the isolation level in effect."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from pcli.sandbox.base import Sandbox
from pcli.sandbox.docker_backend import DockerSandbox
from pcli.sandbox.null_backend import NullSandbox
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox


async def probe_docker_available(*, timeout_s: float = 1.5) -> bool:
    """True only for a reachable Docker daemon running Linux containers.
    DockerSandbox shells out to Linux-only assumptions (`python3`, a
    `/workspace` bind mount, `--pids-limit` - a cgroups-only flag) that
    error out immediately against a daemon in Windows-container mode
    (Docker Desktop's other mode, and what GitHub's windows-latest runners
    default to) rather than just failing to start. Checking the daemon's
    OSType here means that setup falls back to RestrictedSubprocessSandbox
    automatically instead of every tool call failing with a cryptic Docker
    CLI error."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            "--format",
            "{{.OSType}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode == 0 and stdout.decode().strip() == "linux"
    except (TimeoutError, OSError):
        return False


async def select_sandbox(
    *,
    backend_override: str = "auto",
    allowed_roots: list[Path] | None = None,
    cpu_limit_s: int | None = 30,
    memory_limit_bytes: int | None = None,
) -> Sandbox:
    """cpu_limit_s/memory_limit_bytes only affect RestrictedSubprocessSandbox
    (POSIX only) - see Settings.sandbox_cpu_limit_s/sandbox_memory_limit_bytes
    for why memory_limit_bytes defaults to None (RLIMIT_AS's virtual-vs-actual
    memory mismatch breaks ordinary Go-based CLI tool calls, not just
    runaway ones)."""
    if backend_override == "docker":
        return DockerSandbox()
    if backend_override == "subprocess":
        return RestrictedSubprocessSandbox(
            allowed_roots=allowed_roots, cpu_seconds=cpu_limit_s, memory_bytes=memory_limit_bytes
        )
    if backend_override == "none":
        return NullSandbox()
    if backend_override not in ("auto", ""):
        raise ValueError(
            f"Unknown sandbox_backend '{backend_override}' — valid values are 'auto', 'docker', "
            "'subprocess', or 'none'. Set sandbox_backend in config.toml or via "
            "PCLI_SANDBOX_BACKEND."
        )

    if await probe_docker_available():
        return DockerSandbox()
    return RestrictedSubprocessSandbox(
        allowed_roots=allowed_roots, cpu_seconds=cpu_limit_s, memory_bytes=memory_limit_bytes
    )
