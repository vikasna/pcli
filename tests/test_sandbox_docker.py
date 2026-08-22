import asyncio
import shutil
import time
from pathlib import Path

import psutil
import pytest

from pcli.sandbox.base import ExecRequest
from pcli.sandbox.docker_backend import DockerSandbox
from pcli.sandbox.selector import probe_docker_available

pytestmark = pytest.mark.asyncio


async def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return await probe_docker_available()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")
async def test_docker_sandbox_runs_command(tmp_path: Path):
    if not await _docker_ready():
        pytest.skip("docker daemon not reachable")
    sandbox = DockerSandbox(image="python:3.12-slim")
    result = await sandbox.execute(
        ExecRequest(command=["python3", "-c", "print('hi from docker')"], cwd=tmp_path, timeout_s=60)
    )
    assert result.exit_code == 0
    assert "hi from docker" in result.stdout
    assert result.backend_used == "docker"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")
async def test_cancelling_the_awaiting_task_kills_the_docker_run_process(tmp_path: Path):
    """Same regression as subprocess_backend.py's equivalent test: execute()
    used to only catch asyncio.TimeoutError, orphaning the `docker run`
    process (and its container) if the caller cancelled the task instead."""
    if not await _docker_ready():
        pytest.skip("docker daemon not reachable")
    sandbox = DockerSandbox(image="python:3.12-slim")
    task = asyncio.ensure_future(
        sandbox.execute(
            ExecRequest(command=["python3", "-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout_s=120)
        )
    )

    deadline = time.monotonic() + 30
    proc = None
    while time.monotonic() < deadline:
        # Find the local `docker run` process this test started, once it
        # exists — its pid isn't returned by execute() until it completes.
        for candidate in psutil.process_iter(["pid", "name", "cmdline"]):
            cmdline = candidate.info.get("cmdline") or []
            if "run" in cmdline and any(str(tmp_path) in part or part == "docker" for part in cmdline):
                proc = candidate
                break
        if proc is not None:
            break
        await asyncio.sleep(0.2)
    assert proc is not None, "never observed the docker run process start"
    pid = proc.pid

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not psutil.pid_exists(pid), "cancelling the task must kill the docker run process"
