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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pcli.sandbox.base import (
    ExecRequest,
    ExecResult,
    Sandbox,
    SandboxCapabilities,
    SandboxSecurityError,
)
from pcli.sandbox.limits import is_posix, kill_process_tree, make_posix_preexec_fn

_MAX_BACKGROUND_JOB_LIFETIME_S = 2 * 60 * 60
_BACKGROUND_DRAIN_CHUNK_BYTES = 4096

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


async def _pump_stream(stream: asyncio.StreamReader, chunks: list[bytes]) -> None:
    """Reads a stream into `chunks` (owned by the caller) as data arrives,
    rather than accumulating in a local variable the way proc.communicate()
    does — a coroutine cancelled mid-read (our own timeout, or the caller
    cancelling the awaiting task) loses whatever's only in its own locals,
    but `chunks` already holds everything read up to that point. Capping to
    max_output_bytes is left to _truncate() on the joined result, same as
    before this pumped-into-a-list approach replaced proc.communicate()."""
    while True:
        chunk = await stream.read(_BACKGROUND_DRAIN_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)


@dataclass
class BackgroundJob:
    """A command started via RestrictedSubprocessSandbox.start_background:
    runs independently of any single tool call, with its stdout/stderr
    continuously drained into memory (capped at max_output_bytes) so
    read_background can report on it without blocking. stdout_read_offset/
    stderr_read_offset track how much of each stream a caller has already
    consumed (see read_background) — new reads only return what's arrived
    since the last one, rather than re-sending everything or silently
    dropping whatever fell outside a fixed tail window."""

    id: str
    command: str
    started_at: float
    proc: asyncio.subprocess.Process
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_read_offset: int = 0
    stderr_read_offset: int = 0
    exit_code: int | None = None
    finished_at: float | None = None

    @property
    def running(self) -> bool:
        return self.exit_code is None


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
        self._background_jobs: dict[str, BackgroundJob] = {}

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

    def _popen_kwargs(self) -> dict:
        if is_posix():
            return {
                "preexec_fn": make_posix_preexec_fn(
                    cpu_seconds=self._cpu_seconds, memory_bytes=self._memory_bytes
                ),
                "start_new_session": True,
            }
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    async def _start_process(
        self, command: list[str] | str, cwd: Path, env: dict[str, str], *, needs_stdin: bool
    ) -> asyncio.subprocess.Process:
        popen_kwargs = self._popen_kwargs()
        stdin_mode = asyncio.subprocess.PIPE if needs_stdin else asyncio.subprocess.DEVNULL
        if isinstance(command, str):
            return await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                env=env,
                stdin=stdin_mode,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs,
        )

    async def execute(self, request: ExecRequest) -> ExecResult:
        cwd = self._validate_cwd(request.cwd)
        env = _scrub_env(request.env)
        proc = await self._start_process(
            request.command, cwd, env, needs_stdin=request.stdin is not None
        )

        timed_out = False
        stdin_bytes = request.stdin.encode() if request.stdin is not None else None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        # Pumped via separate tasks (not proc.communicate()) specifically so
        # a timeout/cancellation below still leaves whatever was already
        # read sitting in stdout_chunks/stderr_chunks — communicate() would
        # discard it all, since its accumulated bytes live in a coroutine
        # local that's thrown away when the coroutine is cancelled.
        stdout_task = asyncio.ensure_future(_pump_stream(proc.stdout, stdout_chunks))
        stderr_task = asyncio.ensure_future(_pump_stream(proc.stderr, stderr_chunks))
        try:
            if stdin_bytes is not None:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, proc.wait()),
                timeout=request.timeout_s,
            )
        except TimeoutError:
            timed_out = True
            kill_process_tree(proc.pid)
            stdout_task.cancel()
            stderr_task.cancel()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                pass
        except asyncio.CancelledError:
            # Distinct from the TimeoutError case above: this fires when the
            # *caller* (e.g. the TUI's Esc+Esc turn cancellation) cancels the
            # awaiting task, not when our own wait_for's timeout expires.
            # Without this, the subprocess is silently orphaned — cancelling
            # the Python await here does nothing to the OS process itself.
            kill_process_tree(proc.pid)
            stdout_task.cancel()
            stderr_task.cancel()
            raise

        stdout_bytes = b"".join(stdout_chunks)
        stderr_bytes = b"".join(stderr_chunks)
        if timed_out:
            note = b"[pcli] command timed out and was killed"
            stderr_bytes = stderr_bytes + b"\n" + note if stderr_bytes else note

        return ExecResult(
            stdout=_truncate(stdout_bytes, self._max_output_bytes),
            stderr=_truncate(stderr_bytes, self._max_output_bytes),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
            backend_used=self.name,
        )

    # --- Background jobs: run_shell_background/read_background_output/
    # stop_background_process (tools/builtin/shell_tool.py) sit on top of
    # these. Deliberately not part of the Sandbox ABC (see plan) - kept here
    # so DockerSandbox/NullSandbox don't need to implement something they
    # can't meaningfully support yet; callers duck-type-check
    # isinstance(sandbox, RestrictedSubprocessSandbox).

    async def _pump_background_stream(
        self, stream: asyncio.StreamReader, job: BackgroundJob, attr: str
    ) -> None:
        chunks: list[bytes] = getattr(job, f"{attr}_chunks")
        while True:
            chunk = await stream.read(_BACKGROUND_DRAIN_CHUNK_BYTES)
            if not chunk:
                break
            current = getattr(job, f"{attr}_bytes")
            if current < self._max_output_bytes:
                chunks.append(chunk[: self._max_output_bytes - current])
            setattr(job, f"{attr}_bytes", current + len(chunk))

    async def _drain_background(self, job: BackgroundJob) -> None:
        await asyncio.gather(
            self._pump_background_stream(job.proc.stdout, job, "stdout"),
            self._pump_background_stream(job.proc.stderr, job, "stderr"),
        )
        job.exit_code = await job.proc.wait()
        job.finished_at = time.monotonic()

    def _sweep_background_jobs(self, *, keep_finished: int = 20) -> None:
        now = time.monotonic()
        for job in list(self._background_jobs.values()):
            if job.running and now - job.started_at > _MAX_BACKGROUND_JOB_LIFETIME_S:
                kill_process_tree(job.proc.pid)
        finished = sorted(
            (j for j in self._background_jobs.values() if not j.running),
            key=lambda j: j.finished_at or 0.0,
        )
        while len(self._background_jobs) > keep_finished and finished:
            del self._background_jobs[finished.pop(0).id]

    async def start_background(
        self,
        *,
        command: list[str] | str,
        cwd: Path,
        env: dict[str, str] | None = None,
        max_jobs: int = 5,
    ) -> str:
        self._sweep_background_jobs()
        running = sum(1 for job in self._background_jobs.values() if job.running)
        if running >= max_jobs:
            raise SandboxSecurityError(
                f"Already {running} background job(s) running (max {max_jobs}) — stop one "
                "with stop_background_process before starting another."
            )
        resolved_cwd = self._validate_cwd(cwd)
        scrubbed_env = _scrub_env(env or {})
        proc = await self._start_process(command, resolved_cwd, scrubbed_env, needs_stdin=False)
        job_id = uuid.uuid4().hex[:8]
        command_str = command if isinstance(command, str) else " ".join(command)
        job = BackgroundJob(id=job_id, command=command_str, started_at=time.monotonic(), proc=proc)
        self._background_jobs[job_id] = job
        asyncio.create_task(self._drain_background(job))
        return job_id

    def get_background(self, job_id: str) -> BackgroundJob | None:
        return self._background_jobs.get(job_id)

    def read_background(
        self, job_id: str, *, max_chars: int = 4000, reset: bool = False
    ) -> tuple[BackgroundJob, str, str, bool] | None:
        """Returns (job, new_stdout, new_stderr, has_more): only the text
        that arrived since the caller's last read (or from the start, if
        reset or this is the first read), capped to max_chars per stream
        per call — offset-based rather than tail-based, so nothing in the
        middle is silently dropped between polls on a chatty command; a
        caller wanting more just calls again (has_more says so) instead of
        requesting a bigger max_chars, so one poll can't flood context."""
        job = self._background_jobs.get(job_id)
        if job is None:
            return None
        if reset:
            job.stdout_read_offset = 0
            job.stderr_read_offset = 0
        full_stdout = b"".join(job.stdout_chunks).decode(errors="replace")
        full_stderr = b"".join(job.stderr_chunks).decode(errors="replace")
        new_stdout = full_stdout[job.stdout_read_offset :]
        new_stderr = full_stderr[job.stderr_read_offset :]
        stdout_chunk = new_stdout[:max_chars]
        stderr_chunk = new_stderr[:max_chars]
        job.stdout_read_offset += len(stdout_chunk)
        job.stderr_read_offset += len(stderr_chunk)
        has_more = len(new_stdout) > len(stdout_chunk) or len(new_stderr) > len(stderr_chunk)
        return job, stdout_chunk, stderr_chunk, has_more

    async def stop_background(self, job_id: str) -> bool:
        job = self._background_jobs.get(job_id)
        if job is None:
            return False
        if job.running:
            kill_process_tree(job.proc.pid)
            try:
                await asyncio.wait_for(job.proc.wait(), timeout=5)
            except TimeoutError:
                pass
        return True

    async def kill_all_background_jobs(self) -> None:
        """Called from ChatScreen.on_unmount so a background job never
        outlives the TUI process it was started from."""
        for job in list(self._background_jobs.values()):
            if job.running:
                kill_process_tree(job.proc.pid)
