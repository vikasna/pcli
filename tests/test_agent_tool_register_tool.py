"""Coverage for register_agent_tool: validates allowed_tools against the
live registry, persists, merges immediately into ctx.tool_registry, and
survives a simulated restart (persisted store reloaded into a fresh
registry) — mirroring register_toolbox_tool's tests but for the
subagent-persona mechanism."""

from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.agent_tools_store import load_persisted_agent_tools, read_agent_tools
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.builtin.agent_tool_register_tool import REGISTER_AGENT_TOOL
from pcli.tools.registry import ToolRegistry


class _FakeSandbox(Sandbox):
    name = "fake"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("no real tool execution needed for these tests")


async def _noop_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="echoed")


def _make_registry(*tool_names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in tool_names:
        registry.register(
            ToolSpec(
                name=name,
                description="Echo.",
                parameters={"type": "object", "properties": {}},
                handler=_noop_handler,
                needs_permission=False,
            )
        )
    return registry


def _make_ctx(tmp_path: Path, registry: ToolRegistry) -> ToolContext:
    return ToolContext(sandbox=_FakeSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, tool_registry=registry)


@pytest.mark.asyncio
async def test_register_agent_tool_missing_registry_reports_error(tmp_path: Path):
    ctx = ToolContext(sandbox=_FakeSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await REGISTER_AGENT_TOOL.handler(
        {"name": "t", "description": "d", "persona_prompt": "p", "allowed_tools": []}, ctx
    )
    assert result.is_error is True
    assert "aren't available" in result.output


@pytest.mark.asyncio
async def test_register_agent_tool_rejects_unknown_tool_names(tmp_path: Path):
    registry = _make_registry("read_file")
    ctx = _make_ctx(tmp_path, registry)

    result = await REGISTER_AGENT_TOOL.handler(
        {
            "name": "explorer",
            "description": "d",
            "persona_prompt": "p",
            "allowed_tools": ["read_file", "totally_made_up_tool"],
        },
        ctx,
    )

    assert result.is_error is True
    assert "totally_made_up_tool" in result.output
    assert "[pcli] Suggestion:" in result.output
    # Nothing persisted or merged on the rejected call.
    assert read_agent_tools() == {}
    assert "explorer" not in registry


@pytest.mark.asyncio
async def test_register_agent_tool_persists_and_merges_immediately(tmp_path: Path):
    registry = _make_registry("read_file", "grep")
    ctx = _make_ctx(tmp_path, registry)

    result = await REGISTER_AGENT_TOOL.handler(
        {
            "name": "log_explorer",
            "description": "Explores logs.",
            "persona_prompt": "You explore logs.",
            "allowed_tools": ["read_file", "grep"],
            "plan_mode_safe": True,
        },
        ctx,
    )

    assert result.is_error is False
    assert "log_explorer" in result.output

    # Merged immediately - callable in this same session without a restart.
    assert "log_explorer" in registry
    assert registry.get("log_explorer").plan_mode_safe is True

    # Persisted to disk.
    stored = read_agent_tools()
    assert stored["log_explorer"]["allowed_tools"] == ["read_file", "grep"]


@pytest.mark.asyncio
async def test_registered_agent_tool_survives_a_simulated_restart(tmp_path: Path):
    registry = _make_registry("read_file")
    ctx = _make_ctx(tmp_path, registry)

    await REGISTER_AGENT_TOOL.handler(
        {
            "name": "restart_survivor",
            "description": "d",
            "persona_prompt": "p",
            "allowed_tools": ["read_file"],
        },
        ctx,
    )

    # Simulate a fresh process: a brand-new registry reloaded purely from disk.
    reloaded = load_persisted_agent_tools()
    assert "restart_survivor" in reloaded


@pytest.mark.asyncio
async def test_register_agent_tool_always_needs_permission():
    assert REGISTER_AGENT_TOOL.needs_permission is True
