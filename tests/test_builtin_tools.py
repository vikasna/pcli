from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.fs_tools import GLOB_SEARCH, LIST_DIR, READ_FILE, WRITE_FILE
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


@pytest.mark.asyncio
async def test_list_dir(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    ctx = _ctx(tmp_path)
    result = await LIST_DIR.handler({"path": "."}, ctx)
    assert "a.txt" in result.output
    assert "sub" in result.output


@pytest.mark.asyncio
async def test_glob_search(tmp_path: Path):
    (tmp_path / "foo.py").write_text("")
    (tmp_path / "bar.txt").write_text("")
    ctx = _ctx(tmp_path)
    result = await GLOB_SEARCH.handler({"pattern": "*.py"}, ctx)
    assert "foo.py" in result.output
    assert "bar.txt" not in result.output


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
