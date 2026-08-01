import sys
from pathlib import Path

import pytest

from pcli.sandbox.base import ExecRequest, SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox

_ECHO = [sys.executable, "-c", "import sys; print(sys.argv[1])", "hello-sandbox"]


@pytest.mark.asyncio
async def test_execute_argv_list(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    result = await sandbox.execute(ExecRequest(command=_ECHO, cwd=tmp_path, timeout_s=10))
    assert result.exit_code == 0
    assert "hello-sandbox" in result.stdout
    assert result.backend_used == "subprocess"


@pytest.mark.asyncio
async def test_execute_shell_string(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    command = f'"{sys.executable}" -c "print(1 + 1)"'
    result = await sandbox.execute(ExecRequest(command=command, cwd=tmp_path, timeout_s=10))
    assert result.exit_code == 0
    assert "2" in result.stdout


@pytest.mark.asyncio
async def test_cwd_outside_allowed_roots_rejected(tmp_path: Path):
    other_dir = tmp_path.parent
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path / "project"])
    (tmp_path / "project").mkdir()
    with pytest.raises(SandboxSecurityError):
        await sandbox.execute(ExecRequest(command=_ECHO, cwd=other_dir, timeout_s=10))


@pytest.mark.asyncio
async def test_env_scrubbing_hides_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PCLI_TEST_SECRET_KEY", "super-secret-value")
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    script = "import os; print('SECRET=' + os.environ.get('PCLI_TEST_SECRET_KEY', '<absent>'))"
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=10)
    )
    assert "SECRET=<absent>" in result.stdout


@pytest.mark.asyncio
async def test_timeout_kills_process(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    script = "import time; time.sleep(30)"
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=1)
    )
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_output_truncation(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path], max_output_bytes=100)
    script = "print('x' * 1000)"
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=10)
    )
    assert "[...output truncated...]" in result.stdout
    assert len(result.stdout) < 1000
