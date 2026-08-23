"""Coverage for background shell execution: RestrictedSubprocessSandbox's
start_background/read_background/stop_background, and the three tools built
on top of them (run_shell_background/read_background_output/
stop_background_process) in tools/builtin/shell_tool.py.

Unlike run_shell, these actually spawn real subprocesses (no stub sandbox
makes sense here — the whole point under test is the drain/offset/lifecycle
behavior of a genuinely running process)."""

import asyncio
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext
from pcli.tools.builtin.shell_tool import (
    READ_BACKGROUND_OUTPUT,
    RUN_SHELL_BACKGROUND,
    STOP_BACKGROUND_PROCESS,
)


def _ctx(cwd: Path, sandbox) -> ToolContext:
    return ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=cwd)


async def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(interval)


# --- Sandbox-level ---


@pytest.mark.asyncio
async def test_start_background_returns_immediately_and_job_completes(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    job_id = await sandbox.start_background(
        command=[sys.executable, "-c", "print('hello')"], cwd=tmp_path
    )
    assert job_id

    job = sandbox.get_background(job_id)
    assert job is not None
    await _wait_until(lambda: not job.running)
    assert job.exit_code == 0


@pytest.mark.asyncio
async def test_read_background_is_offset_based_not_tail_based(tmp_path: Path):
    """The core correctness property: a poll only ever returns text that
    arrived since the previous poll, never re-delivering old output or
    silently dropping whatever fell outside a fixed tail window."""
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    script = "import time\nfor i in range(5):\n    print(f'line-{i}')\n    time.sleep(0.05)\n"
    job_id = await sandbox.start_background(command=[sys.executable, "-c", script], cwd=tmp_path)
    job = sandbox.get_background(job_id)
    await _wait_until(lambda: not job.running)

    first = sandbox.read_background(job_id, max_chars=4000)
    assert first is not None
    _job, stdout1, _stderr1, has_more1 = first
    assert "line-0" in stdout1
    assert "line-4" in stdout1
    assert has_more1 is False

    # A second read after everything already arrived gets nothing new.
    second = sandbox.read_background(job_id, max_chars=4000)
    assert second is not None
    _job, stdout2, _stderr2, _has_more2 = second
    assert stdout2 == ""


@pytest.mark.asyncio
async def test_read_background_paginates_via_max_chars_and_has_more(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    job_id = await sandbox.start_background(
        command=[sys.executable, "-c", "print('x' * 100)"], cwd=tmp_path
    )
    job = sandbox.get_background(job_id)
    await _wait_until(lambda: not job.running)

    first = sandbox.read_background(job_id, max_chars=40)
    assert first is not None
    _job, stdout1, _stderr1, has_more1 = first
    assert len(stdout1) == 40
    assert has_more1 is True

    second = sandbox.read_background(job_id, max_chars=4000)
    assert second is not None
    _job, stdout2, _stderr2, has_more2 = second
    # 100 'x' chars + whatever print()'s newline is on this platform (\n or
    # \r\n) — total combined length is what actually matters here.
    assert len(stdout1) + len(stdout2) == 100 + len(os.linesep)
    assert has_more2 is False


@pytest.mark.asyncio
async def test_read_background_reset_rereads_from_the_start(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    job_id = await sandbox.start_background(command=[sys.executable, "-c", "print('hi')"], cwd=tmp_path)
    job = sandbox.get_background(job_id)
    await _wait_until(lambda: not job.running)

    sandbox.read_background(job_id, max_chars=4000)  # consume it
    again = sandbox.read_background(job_id, max_chars=4000, reset=True)
    assert again is not None
    _job, stdout, _stderr, _has_more = again
    assert "hi" in stdout


@pytest.mark.asyncio
async def test_read_background_unknown_job_returns_none(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    assert sandbox.read_background("nonexistent") is None


@pytest.mark.asyncio
async def test_start_background_rejects_over_the_concurrent_job_cap(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    script = "import time; time.sleep(5)"
    for _ in range(2):
        await sandbox.start_background(command=[sys.executable, "-c", script], cwd=tmp_path)

    with pytest.raises(SandboxSecurityError):
        await sandbox.start_background(command=[sys.executable, "-c", script], cwd=tmp_path, max_jobs=2)


@pytest.mark.asyncio
async def test_stop_background_kills_the_running_process(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    pid_file = tmp_path / "pid.txt"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    job_id = await sandbox.start_background(command=[sys.executable, "-c", script], cwd=tmp_path)
    await _wait_until(lambda: pid_file.exists())
    pid = int(pid_file.read_text())
    assert psutil.pid_exists(pid)

    stopped = await sandbox.stop_background(job_id)
    assert stopped is True
    await _wait_until(lambda: not psutil.pid_exists(pid))
    assert not psutil.pid_exists(pid)

    job = sandbox.get_background(job_id)
    assert job is not None
    await _wait_until(lambda: not job.running)


@pytest.mark.asyncio
async def test_stop_background_unknown_job_returns_false(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    assert await sandbox.stop_background("nonexistent") is False


@pytest.mark.asyncio
async def test_kill_all_background_jobs_stops_everything_running(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    pid_file = tmp_path / "pid.txt"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    await sandbox.start_background(command=[sys.executable, "-c", script], cwd=tmp_path)
    await _wait_until(lambda: pid_file.exists())
    pid = int(pid_file.read_text())

    await sandbox.kill_all_background_jobs()
    await _wait_until(lambda: not psutil.pid_exists(pid))
    assert not psutil.pid_exists(pid)


# --- Tool-level ---


@pytest.mark.asyncio
async def test_run_shell_background_tool_starts_a_job(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    ctx = _ctx(tmp_path, sandbox)
    result = await RUN_SHELL_BACKGROUND.handler({"command": "echo hi"}, ctx)
    assert result.is_error is False
    assert "Started background job" in result.output


@pytest.mark.asyncio
async def test_run_shell_background_tool_rejects_unsupported_backend(tmp_path: Path):
    from pcli.sandbox.null_backend import NullSandbox

    ctx = _ctx(tmp_path, NullSandbox())
    result = await RUN_SHELL_BACKGROUND.handler({"command": "echo hi"}, ctx)
    assert result.is_error is True
    assert "subprocess" in result.output


@pytest.mark.asyncio
async def test_read_background_output_tool_reports_status_and_new_output(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    ctx = _ctx(tmp_path, sandbox)
    started = await RUN_SHELL_BACKGROUND.handler({"command": "echo hi"}, ctx)
    job_id = started.output.split("'")[1]

    await _wait_until(
        lambda: sandbox.get_background(job_id) is not None and not sandbox.get_background(job_id).running
    )

    result = await READ_BACKGROUND_OUTPUT.handler({"job_id": job_id}, ctx)
    assert result.is_error is False
    assert "exited (exit_code=0)" in result.output
    assert "hi" in result.output


@pytest.mark.asyncio
async def test_read_background_output_tool_unknown_job_is_error(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    ctx = _ctx(tmp_path, sandbox)
    result = await READ_BACKGROUND_OUTPUT.handler({"job_id": "nonexistent"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "run_shell_background" in result.output


@pytest.mark.asyncio
async def test_stop_background_process_tool_stops_a_job(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    ctx = _ctx(tmp_path, sandbox)
    script = "import time; time.sleep(30)"
    started = await RUN_SHELL_BACKGROUND.handler({"command": f'"{sys.executable}" -c "{script}"'}, ctx)
    job_id = started.output.split("'")[1]

    result = await STOP_BACKGROUND_PROCESS.handler({"job_id": job_id}, ctx)
    assert result.is_error is False
    assert "Stopped" in result.output


@pytest.mark.asyncio
async def test_stop_background_process_tool_unknown_job_is_error(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    ctx = _ctx(tmp_path, sandbox)
    result = await STOP_BACKGROUND_PROCESS.handler({"job_id": "nonexistent"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "run_shell_background" in result.output
