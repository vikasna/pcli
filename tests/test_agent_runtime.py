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


@pytest.mark.asyncio
async def test_build_agent_runtime_threads_sandbox_resource_limits_from_settings(tmp_path: Path):
    """Regression coverage for a real reported bug: without this wiring,
    Settings.sandbox_cpu_limit_s/sandbox_memory_limit_bytes would be
    configurable but never actually reach the constructed sandbox."""
    settings = _settings(sandbox_cpu_limit_s=5, sandbox_memory_limit_bytes=64 * 1024 * 1024)
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.sandbox._cpu_seconds == 5
    assert runtime.sandbox._memory_bytes == 64 * 1024 * 1024
    await runtime.client.aclose()


@pytest.mark.asyncio
async def test_build_agent_runtime_defaults_to_no_sandbox_memory_limit(tmp_path: Path):
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.sandbox._memory_bytes is None
    assert runtime.sandbox._cpu_seconds == 30
    await runtime.client.aclose()


@pytest.mark.asyncio
async def test_build_agent_runtime_attaches_a_browser_session_that_is_not_started_yet(
    tmp_path: Path,
):
    """Cheap to hold even when nothing ever uses it - the browser process
    only launches on a browser_* tool's first real call (see
    test_browser_session.py) - so build_agent_runtime always attaches one,
    it never needs its own conditional the way ask_artifact's local-api
    filter does."""
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.browser_session is not None
    assert runtime.browser_session.is_started is False
    await runtime.client.aclose()


@pytest.mark.asyncio
async def test_build_agent_runtime_defaults_the_browser_to_headed(tmp_path: Path):
    """The right default for an interactive TUI session (seeing the browser
    work is part of what makes it trustworthy to watch) - `pcli run` passes
    browser_headless=True explicitly instead (see test_cli_run.py)."""
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)

    assert runtime.browser_session.headless is False
    await runtime.client.aclose()


@pytest.mark.asyncio
async def test_build_agent_runtime_honors_browser_headless_override(tmp_path: Path):
    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path, browser_headless=True)

    assert runtime.browser_session.headless is True
    await runtime.client.aclose()


@pytest.mark.asyncio
async def test_make_tool_context_threads_the_runtimes_browser_session(tmp_path: Path):
    from pcli.agent.runtime import make_tool_context
    from pcli.session.store import SessionStore

    settings = _settings()
    runtime = await build_agent_runtime(settings, tmp_path)
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url=settings.gateway_base_url)

    ctx = make_tool_context(
        runtime,
        settings,
        tmp_path,
        session=session,
        permission_manager=build_permission_manager(settings),
    )

    assert ctx.browser_session is runtime.browser_session
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
