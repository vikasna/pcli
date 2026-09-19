import sys
from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext
from pcli.tools.builtin.pip_tool import PIP_INSTALL


class _StubSandbox(Sandbox):
    name = "stub"

    def __init__(self, result: ExecResult) -> None:
        self._result = result
        self.last_request: ExecRequest | None = None

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        self.last_request = request
        return self._result


class _StubSubprocessSandbox(RestrictedSubprocessSandbox):
    """Same recording behavior as _StubSandbox, but a real
    RestrictedSubprocessSandbox subclass so isinstance() checks in
    pip_tool.py see it as the subprocess backend."""

    def __init__(self, result: ExecResult) -> None:
        super().__init__()
        self._stub_result = result
        self.last_request: ExecRequest | None = None

    async def execute(self, request: ExecRequest) -> ExecResult:
        self.last_request = request
        return self._stub_result


def _ok_result() -> ExecResult:
    return ExecResult(stdout="Successfully installed", stderr="", exit_code=0, timed_out=False, backend_used="stub")


def _ctx(cwd: Path, sandbox: Sandbox) -> ToolContext:
    return ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=cwd)


@pytest.mark.asyncio
async def test_requires_packages_or_requirements_file(tmp_path: Path):
    sandbox = _StubSandbox(_ok_result())
    result = await PIP_INSTALL.handler({}, _ctx(tmp_path, sandbox))
    assert result.is_error
    assert sandbox.last_request is None


@pytest.mark.asyncio
async def test_builds_pip_install_argv_for_packages(tmp_path: Path):
    sandbox = _StubSandbox(_ok_result())
    result = await PIP_INSTALL.handler({"packages": ["requests", "numpy>=1.26"]}, _ctx(tmp_path, sandbox))
    assert not result.is_error
    assert sandbox.last_request is not None
    assert sandbox.last_request.command[1:] == ["-m", "pip", "install", "requests", "numpy>=1.26"]


@pytest.mark.asyncio
async def test_builds_pip_install_argv_for_requirements_file(tmp_path: Path):
    sandbox = _StubSandbox(_ok_result())
    result = await PIP_INSTALL.handler(
        {"requirements_file": "reqs.txt"}, _ctx(tmp_path, sandbox)
    )
    assert not result.is_error
    assert sandbox.last_request.command[1:] == ["-m", "pip", "install", "-r", "reqs.txt"]


@pytest.mark.asyncio
async def test_combines_requirements_file_and_packages(tmp_path: Path):
    sandbox = _StubSandbox(_ok_result())
    await PIP_INSTALL.handler(
        {"requirements_file": "reqs.txt", "packages": ["requests"]}, _ctx(tmp_path, sandbox)
    )
    assert sandbox.last_request.command[1:] == ["-m", "pip", "install", "-r", "reqs.txt", "requests"]


@pytest.mark.asyncio
async def test_requests_network_access_for_the_sandbox_request(tmp_path: Path):
    sandbox = _StubSandbox(_ok_result())
    await PIP_INSTALL.handler({"packages": ["requests"]}, _ctx(tmp_path, sandbox))
    assert sandbox.last_request.network is True


@pytest.mark.asyncio
async def test_uses_sys_executable_under_the_subprocess_sandbox(tmp_path: Path):
    sandbox = _StubSubprocessSandbox(_ok_result())
    await PIP_INSTALL.handler({"packages": ["requests"]}, _ctx(tmp_path, sandbox))
    assert sandbox.last_request.command[0] == sys.executable


@pytest.mark.asyncio
async def test_uses_bare_python_under_a_non_subprocess_sandbox(tmp_path: Path):
    """Under Docker (or any non-subprocess backend), sys.executable is a host
    path that doesn't resolve inside the container - fall back to whatever
    "python" the image itself provides."""
    sandbox = _StubSandbox(_ok_result())
    await PIP_INSTALL.handler({"packages": ["requests"]}, _ctx(tmp_path, sandbox))
    assert sandbox.last_request.command[0] == "python"


@pytest.mark.asyncio
async def test_nonzero_exit_is_error(tmp_path: Path):
    sandbox = _StubSandbox(
        ExecResult(stdout="", stderr="No matching distribution", exit_code=1, timed_out=False, backend_used="stub")
    )
    result = await PIP_INSTALL.handler({"packages": ["not-a-real-package"]}, _ctx(tmp_path, sandbox))
    assert result.is_error
    assert "No matching distribution" in result.output


@pytest.mark.asyncio
async def test_timeout_s_is_clamped_to_the_guardrail_ceiling(tmp_path: Path):
    sandbox = _StubSandbox(_ok_result())
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(max_shell_timeout_s=60), cwd=tmp_path)
    await PIP_INSTALL.handler({"packages": ["requests"], "timeout_s": 9999}, ctx)
    assert sandbox.last_request.timeout_s == 60


@pytest.mark.asyncio
async def test_timed_out_result_suggests_raising_timeout(tmp_path: Path):
    sandbox = _StubSandbox(
        ExecResult(stdout="", stderr="", exit_code=-1, timed_out=True, backend_used="stub")
    )
    result = await PIP_INSTALL.handler({"packages": ["torch"]}, _ctx(tmp_path, sandbox))
    assert result.is_error
    assert "timed out" in result.output.lower()


def test_tool_metadata():
    assert PIP_INSTALL.needs_permission is True
    assert PIP_INSTALL.needs_sandbox is True
    assert PIP_INSTALL.guardrail_path_arg == "requirements_file"
