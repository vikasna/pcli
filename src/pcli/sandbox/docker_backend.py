"""Docker-backed sandbox: one-shot `docker run --rm` per invocation, network
off by default, bind-mounted working directory, resource limits.

Shells out to the `docker` CLI rather than depending on the `docker` SDK, so
this module has no hard dependency beyond the `docker` binary being on PATH.
"""

from __future__ import annotations

import asyncio

from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities


def _truncate(data: bytes, max_bytes: int) -> str:
    if len(data) <= max_bytes:
        return data.decode(errors="replace")
    return data[:max_bytes].decode(errors="replace") + "\n[...output truncated...]"


class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        memory: str = "512m",
        cpus: str = "1",
        pids_limit: int = 256,
        max_output_bytes: int = 2_000_000,
    ) -> None:
        self._image = image
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._max_output_bytes = max_output_bytes

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supports_network_isolation=True, supports_memory_limit=True, supports_cpu_limit=True
        )

    async def execute(self, request: ExecRequest) -> ExecResult:
        cwd = request.cwd.expanduser().resolve()
        docker_args = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "bridge" if request.network else "none",
            "--memory",
            self._memory,
            "--cpus",
            self._cpus,
            "--pids-limit",
            str(self._pids_limit),
            "-v",
            f"{cwd}:/workspace:rw",
            "-w",
            "/workspace",
        ]
        for key, value in request.env.items():
            docker_args += ["-e", f"{key}={value}"]
        docker_args.append(self._image)
        if isinstance(request.command, str):
            docker_args += ["sh", "-c", request.command]
        else:
            docker_args += list(request.command)

        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdin=asyncio.subprocess.PIPE if request.stdin is not None else asyncio.subprocess.DEVNULL,
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
            proc.kill()
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
