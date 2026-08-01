import sys
from pathlib import Path

import pytest

from pcli.tui.shell_passthrough import run_passthrough_command


@pytest.mark.asyncio
async def test_runs_command_and_captures_stdout(tmp_path: Path):
    result = await run_passthrough_command(f'"{sys.executable}" -c "print(1 + 1)"', cwd=tmp_path)
    assert result.exit_code == 0
    assert "2" in result.stdout
    assert result.timed_out is False
    assert result.backend_used == "passthrough"


@pytest.mark.asyncio
async def test_captures_stderr_and_nonzero_exit_code(tmp_path: Path):
    script = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    result = await run_passthrough_command(f'"{sys.executable}" -c "{script}"', cwd=tmp_path)
    assert result.exit_code == 3
    assert "boom" in result.stderr


@pytest.mark.asyncio
async def test_runs_in_given_cwd(tmp_path: Path):
    (tmp_path / "marker.txt").write_text("hi", encoding="utf-8")
    script = "import os; print(os.path.exists('marker.txt'))"
    result = await run_passthrough_command(f'"{sys.executable}" -c "{script}"', cwd=tmp_path)
    assert "True" in result.stdout


@pytest.mark.asyncio
async def test_no_env_scrubbing_unlike_the_sandbox(tmp_path: Path, monkeypatch):
    """Deliberately the opposite of RestrictedSubprocessSandbox: the whole
    point of passthrough is the user's own real environment."""
    monkeypatch.setenv("PCLI_PASSTHROUGH_TEST_VAR", "visible")
    script = "import os; print('VAR=' + os.environ.get('PCLI_PASSTHROUGH_TEST_VAR', '<absent>'))"
    result = await run_passthrough_command(f'"{sys.executable}" -c "{script}"', cwd=tmp_path)
    assert "VAR=visible" in result.stdout


@pytest.mark.asyncio
async def test_timeout_kills_process_and_reports_timed_out(tmp_path: Path):
    script = "import time; time.sleep(30)"
    result = await run_passthrough_command(
        f'"{sys.executable}" -c "{script}"', cwd=tmp_path, timeout_s=1
    )
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_output_truncation(tmp_path: Path):
    from pcli.tui import shell_passthrough as module

    script = "print('x' * 200000)"
    result = await run_passthrough_command(f'"{sys.executable}" -c "{script}"', cwd=tmp_path)
    assert "[...output truncated...]" in result.stdout
    assert len(result.stdout) < 200_000
    assert len(result.stdout) <= module._MAX_OUTPUT_CHARS + len("\n[...output truncated...]")
