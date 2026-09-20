"""Coverage for agent/runtime.py: the UI-agnostic setup ChatScreen, `pcli
run`, and `pcli telegram` all share - sandbox selection, tool registry
construction (with the local-api-only ask_artifact filter), and guardrails/
permission-manager wiring. Isolated automatically by tests/conftest.py's
autouse _isolated_pcli_paths fixture."""

from pathlib import Path

import pytest

from pcli.agent.runtime import (
    build_agent_runtime,
    build_permission_manager,
    effective_max_tool_iterations,
)
from pcli.config.settings import Settings


def _settings(**overrides) -> Settings:
    defaults = {
        "gateway_base_url": "http://fake-gateway.test/v1",
        "gateway_api_key": "test-key",
        "default_model": "fake-model",
        "sandbox_backend": "subprocess",  # skip the real docker probe
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_build_agent_runtime_includes_ask_artifact_in_local_api_mode(tmp_path: Path):
    settings = _settings(local_api_gateways=["http://fake-gateway.test/v1"])
    runtime = await build_agent_runtime(settings, tmp_path)

    assert "ask_artifact" in runtime.tool_registry
    assert "fetch_artifact" in runtime.tool_registry


@pytest.mark.asyncio
async def test_build_agent_runtime_excludes_ask_artifact_outside_local_api_mode(tmp_path: Path):
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert "ask_artifact" not in runtime.tool_registry
    assert "fetch_artifact" in runtime.tool_registry


@pytest.mark.asyncio
async def test_build_agent_runtime_reports_zero_loaded_tools_with_nothing_persisted(
    tmp_path: Path,
):
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.toolbox_tools_loaded == 0
    assert runtime.agent_tools_loaded == 0


@pytest.mark.asyncio
async def test_build_agent_runtime_loads_persisted_agent_tools(tmp_path: Path):
    from pcli.tools.agent_tools_store import save_agent_tool

    save_agent_tool(
        "my_tool",
        description="desc",
        persona_prompt="persona",
        allowed_tools=["read_file"],
        plan_mode_safe=True,
    )

    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.agent_tools_loaded == 1
    assert "my_tool" in runtime.tool_registry


@pytest.mark.asyncio
async def test_build_agent_runtime_sets_up_a_real_sandbox_and_client(tmp_path: Path):
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.sandbox is not None
    assert runtime.sandbox.name == "subprocess"
    assert runtime.client is not None
    await runtime.client.aclose()


def test_effective_max_tool_iterations_uncapped_in_local_api_mode():
    settings = _settings(
        local_api_gateways=["http://fake-gateway.test/v1"], max_tool_iterations=25
    )
    assert effective_max_tool_iterations(settings) is None


def test_effective_max_tool_iterations_uses_the_configured_value_otherwise():
    settings = _settings(max_tool_iterations=42)
    assert effective_max_tool_iterations(settings) == 42


def test_build_permission_manager_uncaps_rate_limits_in_local_api_mode():
    settings = _settings(local_api_gateways=["http://fake-gateway.test/v1"])
    manager = build_permission_manager(settings)

    assert manager.guardrails.max_tool_calls_per_turn == 0
    assert manager.guardrails.max_tool_calls_per_minute == 0


def test_build_permission_manager_leaves_guardrails_untouched_otherwise():
    settings = _settings()
    manager = build_permission_manager(settings)

    assert manager.guardrails.max_tool_calls_per_turn == 25
    assert manager.guardrails.max_tool_calls_per_minute == 60
