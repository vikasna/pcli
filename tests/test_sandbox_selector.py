"""Coverage for select_sandbox's explicit backend_override branches, plus
the error message for an invalid one - previously just "Unknown
sandbox_backend '<value>'" with no indication of what a valid value looks
like or where to fix it."""

import pytest

from pcli.sandbox.docker_backend import DockerSandbox
from pcli.sandbox.null_backend import NullSandbox
from pcli.sandbox.selector import select_sandbox
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox


@pytest.mark.asyncio
async def test_explicit_docker_override():
    sandbox = await select_sandbox(backend_override="docker")
    assert isinstance(sandbox, DockerSandbox)


@pytest.mark.asyncio
async def test_explicit_subprocess_override():
    sandbox = await select_sandbox(backend_override="subprocess")
    assert isinstance(sandbox, RestrictedSubprocessSandbox)


@pytest.mark.asyncio
async def test_subprocess_override_defaults_to_no_memory_limit():
    sandbox = await select_sandbox(backend_override="subprocess")
    assert isinstance(sandbox, RestrictedSubprocessSandbox)
    assert sandbox._memory_bytes is None
    assert sandbox._cpu_seconds == 30


@pytest.mark.asyncio
async def test_subprocess_override_threads_explicit_cpu_and_memory_limits():
    sandbox = await select_sandbox(
        backend_override="subprocess", cpu_limit_s=5, memory_limit_bytes=64 * 1024 * 1024
    )
    assert isinstance(sandbox, RestrictedSubprocessSandbox)
    assert sandbox._cpu_seconds == 5
    assert sandbox._memory_bytes == 64 * 1024 * 1024


@pytest.mark.asyncio
async def test_auto_fallback_to_subprocess_also_threads_cpu_and_memory_limits(
    monkeypatch: pytest.MonkeyPatch,
):
    """The 'auto' path (Docker unreachable -> subprocess fallback) is a
    separate branch from the explicit 'subprocess' override above - must
    thread the same limits, not silently fall back to the class defaults."""
    import pcli.sandbox.selector as selector_module

    async def _docker_unavailable(*, timeout_s: float = 1.5) -> bool:
        return False

    monkeypatch.setattr(selector_module, "probe_docker_available", _docker_unavailable)

    sandbox = await select_sandbox(
        backend_override="auto", cpu_limit_s=5, memory_limit_bytes=64 * 1024 * 1024
    )
    assert isinstance(sandbox, RestrictedSubprocessSandbox)
    assert sandbox._cpu_seconds == 5
    assert sandbox._memory_bytes == 64 * 1024 * 1024


@pytest.mark.asyncio
async def test_explicit_none_override():
    sandbox = await select_sandbox(backend_override="none")
    assert isinstance(sandbox, NullSandbox)


@pytest.mark.asyncio
async def test_unknown_backend_names_the_bad_value_and_valid_options():
    with pytest.raises(ValueError) as exc_info:
        await select_sandbox(backend_override="dckr")  # typo

    message = str(exc_info.value)
    assert "'dckr'" in message
    assert "'auto'" in message and "'docker'" in message and "'subprocess'" in message and "'none'" in message
    assert "sandbox_backend" in message
    assert "PCLI_SANDBOX_BACKEND" in message


class _FakeDockerInfoProcess:
    def __init__(self, stdout: bytes, returncode: int) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


@pytest.mark.asyncio
async def test_probe_docker_available_false_for_a_windows_container_daemon(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression guard: a Docker daemon in Windows-container mode (Docker
    Desktop's other mode, and what GitHub's windows-latest CI runners
    default to) used to probe as "available", but DockerSandbox's
    Linux-only assumptions (--pids-limit, a /workspace bind mount, python3)
    then failed every real tool call with a cryptic Docker CLI error."""
    import pcli.sandbox.selector as selector_module

    monkeypatch.setattr(selector_module.shutil, "which", lambda _name: "/usr/bin/docker")

    async def fake_exec(*_args, **_kwargs):
        return _FakeDockerInfoProcess(b"windows\n", 0)

    monkeypatch.setattr(selector_module.asyncio, "create_subprocess_exec", fake_exec)

    assert await selector_module.probe_docker_available() is False


@pytest.mark.asyncio
async def test_probe_docker_available_true_for_a_linux_daemon(monkeypatch: pytest.MonkeyPatch):
    import pcli.sandbox.selector as selector_module

    monkeypatch.setattr(selector_module.shutil, "which", lambda _name: "/usr/bin/docker")

    async def fake_exec(*_args, **_kwargs):
        return _FakeDockerInfoProcess(b"linux\n", 0)

    monkeypatch.setattr(selector_module.asyncio, "create_subprocess_exec", fake_exec)

    assert await selector_module.probe_docker_available() is True
