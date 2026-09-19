from pathlib import Path

import pytest

from pcli.memory.store import read_memory
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.memory_tool import REMEMBER


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("remember should never touch the sandbox")


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path)


@pytest.mark.asyncio
async def test_remember_persists_an_entry(tmp_path: Path):
    result = await REMEMBER.handler(
        {"content": "Works as a backend Python developer", "category": "profile"}, _ctx(tmp_path)
    )

    assert result.is_error is False
    entries = read_memory().entries
    assert len(entries) == 1
    assert entries[0].content == "Works as a backend Python developer"
    assert entries[0].category == "profile"
    assert entries[0].source == "derived"  # default when 'source' is omitted


@pytest.mark.asyncio
async def test_remember_with_explicit_source(tmp_path: Path):
    await REMEMBER.handler(
        {"content": "Uses tabs, not spaces", "category": "preference", "source": "explicit"},
        _ctx(tmp_path),
    )

    entries = read_memory().entries
    assert entries[0].source == "explicit"


@pytest.mark.asyncio
async def test_remember_requires_content(tmp_path: Path):
    result = await REMEMBER.handler({"category": "profile"}, _ctx(tmp_path))
    assert result.is_error is True
    assert read_memory().entries == []


@pytest.mark.asyncio
async def test_remember_requires_a_valid_category(tmp_path: Path):
    result = await REMEMBER.handler({"content": "something", "category": "nonsense"}, _ctx(tmp_path))
    assert result.is_error is True
    assert read_memory().entries == []


@pytest.mark.asyncio
async def test_remember_is_global_not_session_scoped(tmp_path: Path):
    """Unlike record_decision, remember has nothing to do with ctx.session -
    it must work fine (and write to the global store) even with no session
    at all."""
    ctx = ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, session=None)
    result = await REMEMBER.handler({"content": "fact", "category": "profile"}, ctx)

    assert result.is_error is False
    assert len(read_memory().entries) == 1


def test_tool_metadata():
    assert REMEMBER.needs_permission is False
    assert REMEMBER.plan_mode_safe is True
