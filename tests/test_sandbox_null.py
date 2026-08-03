import sys
from pathlib import Path

import pytest

from pcli.sandbox.base import ExecRequest
from pcli.sandbox.null_backend import NullSandbox
from pcli.sandbox.selector import select_sandbox

_ECHO = [sys.executable, "-c", "import sys; print(sys.argv[1])", "hello-null-sandbox"]


@pytest.mark.asyncio
async def test_execute_argv_list(tmp_path: Path):
    sandbox = NullSandbox()
    result = await sandbox.execute(ExecRequest(command=_ECHO, cwd=tmp_path, timeout_s=10))
    assert result.exit_code == 0
    assert "hello-null-sandbox" in result.stdout
    assert result.backend_used == "none"


@pytest.mark.asyncio
async def test_inherits_full_environment_unlike_restricted_sandbox(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PCLI_TEST_NULL_SANDBOX_VAR", "visible-value")
    sandbox = NullSandbox()
    script = "import os; print('VAR=' + os.environ.get('PCLI_TEST_NULL_SANDBOX_VAR', '<absent>'))"
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=10)
    )
    assert "VAR=visible-value" in result.stdout


@pytest.mark.asyncio
async def test_timeout_kills_process(tmp_path: Path):
    sandbox = NullSandbox()
    script = "import time; time.sleep(30)"
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=1)
    )
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_select_sandbox_none_returns_null_sandbox(tmp_path: Path):
    sandbox = await select_sandbox(backend_override="none", allowed_roots=[tmp_path])
    assert isinstance(sandbox, NullSandbox)
    assert sandbox.name == "none"
