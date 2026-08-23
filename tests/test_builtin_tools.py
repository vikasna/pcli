from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.fs_tools import EDIT_FILE, GLOB_SEARCH, LIST_DIR, READ_FILE, WRITE_FILE
from pcli.tools.builtin.grep_tool import GREP
from pcli.tools.builtin.shell_tool import RUN_SHELL


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("should not be called in fs tool tests")


class _StubShellSandbox(Sandbox):
    name = "stub"

    def __init__(self, result: ExecResult) -> None:
        self._result = result
        self.last_request: ExecRequest | None = None

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        self.last_request = request
        return self._result


def _ctx(cwd: Path, sandbox: Sandbox | None = None) -> ToolContext:
    return ToolContext(sandbox=sandbox or _NullSandbox(), guardrails=GuardrailsConfig(), cwd=cwd)


@pytest.mark.asyncio
async def test_read_write_file_roundtrip(tmp_path: Path):
    ctx = _ctx(tmp_path)
    write_result = await WRITE_FILE.handler({"path": "note.txt", "content": "hello"}, ctx)
    assert write_result.is_error is False

    read_result = await READ_FILE.handler({"path": "note.txt"}, ctx)
    assert read_result.output == "hello"


@pytest.mark.asyncio
async def test_read_file_not_found(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = await READ_FILE.handler({"path": "missing.txt"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "list_dir" in result.output or "glob_search" in result.output


@pytest.mark.asyncio
async def test_edit_file_replaces_unique_match(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello world\ngoodbye world\n")
    ctx = _ctx(tmp_path)
    result = await EDIT_FILE.handler(
        {"path": "note.txt", "old_string": "hello world", "new_string": "hi world"}, ctx
    )
    assert result.is_error is False
    assert (tmp_path / "note.txt").read_text() == "hi world\ngoodbye world\n"


@pytest.mark.asyncio
async def test_edit_file_missing_target_errors(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = await EDIT_FILE.handler(
        {"path": "missing.txt", "old_string": "x", "new_string": "y"}, ctx
    )
    assert result.is_error is True
    assert "write_file" in result.output


@pytest.mark.asyncio
async def test_edit_file_old_string_not_found_errors(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello world\n")
    ctx = _ctx(tmp_path)
    result = await EDIT_FILE.handler(
        {"path": "note.txt", "old_string": "not present", "new_string": "y"}, ctx
    )
    assert result.is_error is True
    assert "not found" in result.output
    assert "[pcli] Suggestion:" in result.output
    assert "read_file" in result.output


@pytest.mark.asyncio
async def test_edit_file_ambiguous_match_errors_without_writing(tmp_path: Path):
    (tmp_path / "note.txt").write_text("dup\ndup\n")
    ctx = _ctx(tmp_path)
    result = await EDIT_FILE.handler({"path": "note.txt", "old_string": "dup", "new_string": "x"}, ctx)
    assert result.is_error is True
    assert "not unique" in result.output
    assert (tmp_path / "note.txt").read_text() == "dup\ndup\n"  # untouched


@pytest.mark.asyncio
async def test_list_dir(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    ctx = _ctx(tmp_path)
    result = await LIST_DIR.handler({"path": "."}, ctx)
    assert "a.txt" in result.output
    assert "sub" in result.output


@pytest.mark.asyncio
async def test_list_dir_not_a_directory_suggests_checking_parent(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    ctx = _ctx(tmp_path)
    result = await LIST_DIR.handler({"path": "a.txt"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "list_dir" in result.output


@pytest.mark.asyncio
async def test_glob_search(tmp_path: Path):
    (tmp_path / "foo.py").write_text("")
    (tmp_path / "bar.txt").write_text("")
    ctx = _ctx(tmp_path)
    result = await GLOB_SEARCH.handler({"pattern": "*.py"}, ctx)
    assert "foo.py" in result.output
    assert "bar.txt" not in result.output


@pytest.mark.asyncio
async def test_glob_search_not_a_directory_suggests_checking_parent(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    ctx = _ctx(tmp_path)
    result = await GLOB_SEARCH.handler({"pattern": "*.py", "path": "a.txt"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "list_dir" in result.output


@pytest.mark.asyncio
async def test_grep_finds_matches(tmp_path: Path):
    (tmp_path / "a.py").write_text("def foo():\n    return 42\n")
    (tmp_path / "b.py").write_text("class Bar: pass\n")
    ctx = _ctx(tmp_path)
    result = await GREP.handler({"pattern": r"def \w+"}, ctx)
    assert "a.py:1" in result.output
    assert "b.py" not in result.output


@pytest.mark.asyncio
async def test_grep_invalid_regex(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = await GREP.handler({"pattern": "("}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "substring" in result.output


@pytest.mark.asyncio
async def test_grep_not_a_directory_suggests_list_dir(tmp_path: Path):
    (tmp_path / "file.txt").write_text("x")
    ctx = _ctx(tmp_path)
    result = await GREP.handler({"pattern": "x", "path": "file.txt"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "list_dir" in result.output


@pytest.mark.asyncio
async def test_run_shell_routes_through_sandbox_and_reports_exit_code(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(stdout="hi\n", stderr="", exit_code=0, timed_out=False, backend_used="stub")
    )
    ctx = _ctx(tmp_path, sandbox)
    result = await RUN_SHELL.handler({"command": "echo hi"}, ctx)
    assert result.is_error is False
    assert "hi" in result.output
    assert sandbox.last_request is not None
    assert sandbox.last_request.command == "echo hi"


@pytest.mark.asyncio
async def test_run_shell_nonzero_exit_is_error(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(stdout="", stderr="boom", exit_code=1, timed_out=False, backend_used="stub")
    )
    ctx = _ctx(tmp_path, sandbox)
    result = await RUN_SHELL.handler({"command": "false"}, ctx)
    assert result.is_error is True
    assert "boom" in result.output


@pytest.mark.asyncio
async def test_run_shell_timeout_s_is_clamped_to_the_guardrail_ceiling(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(stdout="ok\n", stderr="", exit_code=0, timed_out=False, backend_used="stub")
    )
    guardrails = GuardrailsConfig(max_shell_timeout_s=60)
    ctx = ToolContext(sandbox=sandbox, guardrails=guardrails, cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "echo ok", "timeout_s": 9999}, ctx)

    assert sandbox.last_request is not None
    assert sandbox.last_request.timeout_s == 60
    assert "clamped" in result.output


@pytest.mark.asyncio
async def test_run_shell_timeout_s_under_the_ceiling_is_unaffected(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(stdout="ok\n", stderr="", exit_code=0, timed_out=False, backend_used="stub")
    )
    guardrails = GuardrailsConfig(max_shell_timeout_s=300)
    ctx = ToolContext(sandbox=sandbox, guardrails=guardrails, cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "echo ok", "timeout_s": 45}, ctx)

    assert sandbox.last_request is not None
    assert sandbox.last_request.timeout_s == 45
    assert "clamped" not in result.output


# --- Corrective suggestions on failure ---
#
# Regression coverage for a real debugged session where the same mistakes
# got rediscovered by trial and error instead of being headed off: three
# separate run_shell timeouts each cost a full extra round-trip even though
# a longer timeout_s had already worked earlier in the same session, and a
# Windows "python3 not found" error was hit twice in a row before the model
# tried plain "python".


@pytest.mark.asyncio
async def test_run_shell_timeout_under_ceiling_suggests_raising_timeout_s(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(stdout="", stderr="", exit_code=15, timed_out=True, backend_used="stub")
    )
    guardrails = GuardrailsConfig(max_shell_timeout_s=300)
    ctx = ToolContext(sandbox=sandbox, guardrails=guardrails, cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "python train.py", "timeout_s": 30}, ctx)

    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "timeout_s" in result.output
    assert "run_shell_background" in result.output


@pytest.mark.asyncio
async def test_run_shell_timeout_at_ceiling_suggests_background_instead(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(stdout="", stderr="", exit_code=15, timed_out=True, backend_used="stub")
    )
    guardrails = GuardrailsConfig(max_shell_timeout_s=60)
    ctx = ToolContext(sandbox=sandbox, guardrails=guardrails, cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "python train.py", "timeout_s": 60}, ctx)

    assert result.is_error is True
    assert "run_shell_background" in result.output
    assert "even at the guardrail ceiling" in result.output


@pytest.mark.asyncio
async def test_run_shell_bare_pip_not_recognized_suggests_python_dash_m_pip(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(
            stdout="",
            stderr="'pip' is not recognized as an internal or external command,\n"
            "operable program or batch file.",
            exit_code=1,
            timed_out=False,
            backend_used="stub",
        )
    )
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "pip install pandas"}, ctx)

    assert "[pcli] Suggestion:" in result.output
    assert "python -m pip" in result.output


@pytest.mark.asyncio
async def test_run_shell_python3_windows_store_stub_suggests_plain_python(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(
            stdout="",
            stderr="Python was not found; run without arguments to install from the "
            "Microsoft Store, or disable this shortcut from Settings > Apps.",
            exit_code=9009,
            timed_out=False,
            backend_used="stub",
        )
    )
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "python3 script.py"}, ctx)

    assert "[pcli] Suggestion:" in result.output
    assert "'python', not 'python3'" in result.output


@pytest.mark.asyncio
async def test_run_shell_module_not_found_suggests_checking_the_interpreter(tmp_path: Path):
    sandbox = _StubShellSandbox(
        ExecResult(
            stdout="",
            stderr="Traceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'pandas'",
            exit_code=1,
            timed_out=False,
            backend_used="stub",
        )
    )
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "python script.py"}, ctx)

    assert "[pcli] Suggestion:" in result.output
    assert "sys.executable" in result.output


@pytest.mark.asyncio
async def test_run_shell_unrecognized_failure_gets_no_suggestion(tmp_path: Path):
    """A failure that doesn't match any known pattern must not get a
    fabricated suggestion — only real matches should add one."""
    sandbox = _StubShellSandbox(
        ExecResult(
            stdout="", stderr="ValueError: shapes not aligned", exit_code=1, timed_out=False, backend_used="stub"
        )
    )
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)

    result = await RUN_SHELL.handler({"command": "python train.py"}, ctx)

    assert result.is_error is True
    assert "[pcli] Suggestion:" not in result.output
