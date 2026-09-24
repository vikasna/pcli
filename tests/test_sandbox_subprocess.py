import asyncio
import sys
import time
from pathlib import Path

import psutil
import pytest

from pcli.sandbox.base import ExecRequest, SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox

_ECHO = [sys.executable, "-c", "import sys; print(sys.argv[1])", "hello-sandbox"]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="preexec_fn/setsid only applies on POSIX"
)


@_posix_only
def test_posix_preexec_fn_does_not_call_setsid_itself():
    """Regression test: RestrictedSubprocessSandbox passes start_new_session=True,
    which makes Popen call os.setsid() itself right after forking, before
    running preexec_fn. The preexec_fn used to call os.setsid() again too,
    which raises EPERM on an already-session-leader process and crashed
    *every* sandboxed shell command on Linux with 'Exception occurred in
    preexec_fn.' — see sandbox/limits.py."""
    from pcli.sandbox.limits import make_posix_preexec_fn

    preexec = make_posix_preexec_fn(cpu_seconds=None, memory_bytes=None)
    assert preexec is not None
    preexec()  # must not raise


def test_default_memory_limit_is_none():
    """Regression coverage for a real reported bug: RLIMIT_AS bounds
    *virtual* address space, not actual usage, and Go's runtime (kubectl,
    terraform, most Go-based CLIs - exactly what toolbox exists to wrap)
    routinely reserves well past a few hundred MB of virtual space at
    startup regardless of real RSS, so the old 512MB default made ordinary
    tool invocations fail outright, not just runaway ones. See
    subprocess_backend.py's own __init__ comment for the full reasoning."""
    sandbox = RestrictedSubprocessSandbox()
    assert sandbox._memory_bytes is None


def test_default_cpu_limit_is_unchanged():
    """cpu_seconds (RLIMIT_CPU) doesn't share RLIMIT_AS's false-positive
    problem - a large virtual reservation at startup burns no CPU time -
    so it stays on by default."""
    sandbox = RestrictedSubprocessSandbox()
    assert sandbox._cpu_seconds == 30


_MMAP_600MB = [
    sys.executable,
    "-c",
    "import mmap; mmap.mmap(-1, 600 * 1024 * 1024); print('reserved-ok')",
]


@_posix_only
@pytest.mark.asyncio
async def test_default_memory_limit_does_not_break_a_large_virtual_reservation(tmp_path: Path):
    """End-to-end reproduction of the reported bug class, portable (stdlib
    mmap, no real Go binary needed): reserving 600MB of *virtual* address
    space (what Go's runtime routinely does at startup, well past the old
    512MB RLIMIT_AS default) must succeed now that memory_bytes defaults
    to None, the same way it would have failed under the old default."""
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    result = await sandbox.execute(ExecRequest(command=_MMAP_600MB, cwd=tmp_path, timeout_s=10))
    assert result.exit_code == 0
    assert "reserved-ok" in result.stdout


@_posix_only
@pytest.mark.asyncio
async def test_explicit_memory_limit_still_enforces_rlimit_as(tmp_path: Path):
    """memory_bytes is still a real, working override for anyone who
    deliberately wants a memory cap back (e.g. on a constrained VM) - not
    removed outright, just no longer forced on everyone by default."""
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path], memory_bytes=64 * 1024 * 1024)
    result = await sandbox.execute(ExecRequest(command=_MMAP_600MB, cwd=tmp_path, timeout_s=10))
    assert result.exit_code != 0
    assert "reserved-ok" not in result.stdout


@pytest.mark.asyncio
async def test_execute_argv_list(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    result = await sandbox.execute(ExecRequest(command=_ECHO, cwd=tmp_path, timeout_s=10))
    assert result.exit_code == 0
    assert "hello-sandbox" in result.stdout
    assert result.backend_used == "subprocess"


@_posix_only
@pytest.mark.asyncio
async def test_execute_shell_string_posix_native_command(tmp_path: Path):
    """Regression test for the preexec_fn/setsid double-call bug (see
    test_posix_preexec_fn_does_not_call_setsid_itself): runs a real shell
    built-in through the actual sandbox, end to end, on POSIX."""
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    result = await sandbox.execute(
        ExecRequest(command="echo hello-from-shell", cwd=tmp_path, timeout_s=10)
    )
    assert result.exit_code == 0
    assert "hello-from-shell" in result.stdout


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
async def test_timeout_preserves_output_already_produced(tmp_path: Path):
    """Regression test: execute() used to read stdout/stderr via
    proc.communicate(), which only returns its accumulated bytes on
    completion. Cancelling it on timeout (asyncio.wait_for) threw away
    *everything* read so far, not just what hadn't arrived yet - a script
    that printed plenty of progress output before running long enough to
    hit the timeout would come back with an empty stdout. Pumping into
    caller-owned lists (see _pump_stream) fixes this: the timeout kills the
    process but the output already read stays put."""
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    script = (
        "import sys, time; "
        "print('before-the-timeout'); sys.stdout.flush(); "
        "time.sleep(30)"
    )
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=1)
    )
    assert result.timed_out is True
    assert "before-the-timeout" in result.stdout
    assert "command timed out and was killed" in result.stderr


@pytest.mark.asyncio
async def test_cancelling_the_awaiting_task_kills_the_subprocess(tmp_path: Path):
    """Regression test: execute()'s try/except used to only catch
    asyncio.TimeoutError (its own internal wait_for timeout) — if the
    *caller* cancels the task instead (e.g. the TUI's Esc+Esc), the
    subprocess was silently orphaned since cancelling the Python await does
    nothing to the OS process by itself."""
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    pid_file = tmp_path / "pid.txt"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    task = asyncio.ensure_future(
        sandbox.execute(ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=30))
    )

    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "subprocess never reported its pid"
    pid = int(pid_file.read_text())
    assert psutil.pid_exists(pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(pid), "cancelling the task must kill the subprocess"


@pytest.mark.asyncio
async def test_output_truncation(tmp_path: Path):
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path], max_output_bytes=100)
    script = "print('x' * 1000)"
    result = await sandbox.execute(
        ExecRequest(command=[sys.executable, "-c", script], cwd=tmp_path, timeout_s=10)
    )
    assert "[...output truncated...]" in result.stdout
    assert len(result.stdout) < 1000
