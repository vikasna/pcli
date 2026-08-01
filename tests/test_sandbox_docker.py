import shutil
from pathlib import Path

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
