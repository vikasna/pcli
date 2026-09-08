"""Coverage for make_agent_tool: the shared mechanism behind the 3 default
exploration agent tools and any user/model-registered ones — restricts a
nested AgentLoop to a fixed allowed-tool set and persona, mirroring
spawn_subagent but with both baked in at registration time."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from pcli.agent.loop import AgentLoop
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.permissions.policy import PermissionPolicy
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.agent_tools import (
    DATA_ANALYSIS,
    DEEP_RESEARCH,
    EXPLORE_CODEBASE,
    EXPLORE_FILES,
    EXPLORE_LOGS,
    VERIFY_COMPUTATION,
    WRITE_DOCUMENTATION,
    make_agent_tool,
)
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.registry import ToolRegistry, build_default_registry


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        max_retries=1,
    )


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str) -> httpx.Response:
    return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}))


class _FakeSandbox(Sandbox):
    name = "fake"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("no real tool execution needed for these tests")


async def _noop_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="echoed")


def _echo_tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Echo.",
        parameters={"type": "object", "properties": {}},
        handler=_noop_handler,
        needs_permission=False,
    )


def _make_registry(*tool_names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in tool_names:
        registry.register(_echo_tool(name))
    return registry


def _make_ctx(
    tmp_path: Path, registry: ToolRegistry, client: GatewayClient, permission_manager: PermissionManager
) -> ToolContext:
    return ToolContext(
        sandbox=_FakeSandbox(),
        guardrails=permission_manager.guardrails,
        cwd=tmp_path,
        gateway_client=client,
        model="fake-model",
        tool_registry=registry,
        permission_manager=permission_manager,
        ask=None,
        max_tool_iterations=25,
    )


@pytest.mark.asyncio
@respx.mock
async def test_make_agent_tool_inherits_max_response_tokens_and_temperature(tmp_path: Path):
    """Regression coverage for a real gap: a fresh AgentLoop defaults both
    to None, so without this a subagent silently ran with no response-
    length cap and the gateway's default temperature regardless of what
    /max-response-tokens or /temperature had configured on the parent."""
    captured_kwargs: list[dict] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_kwargs.append(kwargs)
        return original_init(self, *args, **kwargs)

    import pcli.tools.agent_tools as agent_tools_module

    agent_tools_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = _make_registry("echo_a")
        tool = make_agent_tool("restricted_tool", "desc", "persona", ["echo_a"])

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            ctx = replace(ctx, max_response_tokens=1234, temperature=0.55)
            await tool.handler({"query": "do something"}, ctx)
    finally:
        agent_tools_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert captured_kwargs[0]["max_response_tokens"] == 1234
    assert captured_kwargs[0]["temperature"] == 0.55


@pytest.mark.asyncio
async def test_make_agent_tool_missing_context_reports_error(tmp_path: Path):
    tool = make_agent_tool("t", "desc", "persona", ["echo_a"])
    ctx = ToolContext(sandbox=_FakeSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await tool.handler({"query": "do something"}, ctx)
    assert result.is_error is True
    assert "isn't available" in result.output


@pytest.mark.asyncio
@respx.mock
async def test_make_agent_tool_restricts_nested_subagent_to_allowed_tool_names(tmp_path: Path):
    """Prove the restriction is structural: intercept the nested AgentLoop's
    tool_registry rather than trusting the summary text."""
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.agent_tools as agent_tools_module

    agent_tools_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = _make_registry("echo_a", "echo_b")
        tool = make_agent_tool("restricted_tool", "desc", "persona", ["echo_a"])

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            await tool.handler({"query": "do something"}, ctx)
    finally:
        agent_tools_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    assert len(captured_registries) == 1
    assert "echo_a" in captured_registries[0]
    assert "echo_b" not in captured_registries[0]


@pytest.mark.asyncio
@respx.mock
async def test_make_agent_tool_can_actually_call_an_allowed_tool(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "echo_a", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        ),
        _text_response("Used echo_a successfully."),
    ]

    permission_manager = PermissionManager(
        guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    registry = _make_registry("echo_a")
    tool = make_agent_tool("explorer", "desc", "persona", ["echo_a"])

    async with GatewayClient(_settings()) as client:
        ctx = _make_ctx(tmp_path, registry, client, permission_manager)
        result = await tool.handler({"query": "use echo_a"}, ctx)

    assert result.is_error is False
    assert "explorer made 1 tool call(s)" in result.output
    assert "Used echo_a successfully." in result.output


@pytest.mark.asyncio
async def test_default_agent_tools_are_present_in_build_default_registry():
    registry = build_default_registry()
    assert "explore_codebase" in registry
    assert "explore_files" in registry
    assert "explore_logs" in registry
    assert "register_agent_tool" in registry


@pytest.mark.asyncio
@respx.mock
async def test_explore_codebase_default_is_restricted_to_its_documented_allowed_set(tmp_path: Path):
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.agent_tools as agent_tools_module

    agent_tools_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = build_default_registry()

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            await EXPLORE_CODEBASE.handler({"query": "how does X work"}, ctx)
    finally:
        agent_tools_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    sub_registry = captured_registries[0]
    for name in ("read_file", "list_dir", "glob_search", "grep", "search_python", "inspect_python_module"):
        assert name in sub_registry
    for name in ("write_file", "edit_file", "run_shell", "spawn_subagent"):
        assert name not in sub_registry


@pytest.mark.asyncio
@respx.mock
async def test_explore_logs_default_excludes_run_shell(tmp_path: Path):
    """Deliberate: explore_logs stays uniformly read-only via read_file/grep
    only, never run_shell, even though a log tail could plausibly use it."""
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.agent_tools as agent_tools_module

    agent_tools_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            return_value=_text_response("done")
        )
        permission_manager = PermissionManager(
            guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
        )
        registry = build_default_registry()

        async with GatewayClient(_settings()) as client:
            ctx = _make_ctx(tmp_path, registry, client, permission_manager)
            await EXPLORE_LOGS.handler({"query": "find the first error"}, ctx)
    finally:
        agent_tools_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    sub_registry = captured_registries[0]
    assert "read_file" in sub_registry
    assert "grep" in sub_registry
    assert "run_shell" not in sub_registry
    assert "run_shell_background" not in sub_registry


def test_default_agent_tools_are_plan_mode_safe():
    assert EXPLORE_CODEBASE.plan_mode_safe is True
    assert EXPLORE_FILES.plan_mode_safe is True
    assert EXPLORE_LOGS.plan_mode_safe is True


def test_agent_tools_always_need_permission():
    """Regardless of plan_mode_safe, an agent tool still spawns a nested
    subagent that can call tools on its own — always permission-gated, same
    as spawn_subagent."""
    assert EXPLORE_CODEBASE.needs_permission is True
    unsafe_tool = make_agent_tool("t", "desc", "persona", ["echo_a"], plan_mode_safe=False)
    assert unsafe_tool.needs_permission is True


# --- write_documentation / verify_computation / deep_research / data_analysis ---


async def _captured_sub_registry(tmp_path: Path, tool: ToolSpec, arguments: dict) -> ToolRegistry:
    """Runs `tool` through a real handler call with a spied AgentLoop.__init__
    to capture the actual tool_registry the nested subagent was restricted
    to — same pattern as the explore_* restriction tests above, generalized
    so each new tool doesn't need to repeat the spy setup/teardown."""
    captured_registries: list[ToolRegistry] = []
    original_init = AgentLoop.__init__

    def _spying_init(self, *args, **kwargs):
        captured_registries.append(kwargs.get("tool_registry"))
        return original_init(self, *args, **kwargs)

    import pcli.tools.agent_tools as agent_tools_module

    agent_tools_module.AgentLoop.__init__ = _spying_init  # type: ignore[method-assign]

    try:
        with respx.mock:
            respx.post("http://fake-gateway.test/v1/chat/completions").mock(
                return_value=_text_response("done")
            )
            permission_manager = PermissionManager(
                guardrails=GuardrailsConfig(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
            )
            registry = build_default_registry()

            async with GatewayClient(_settings()) as client:
                ctx = _make_ctx(tmp_path, registry, client, permission_manager)
                await tool.handler(arguments, ctx)
    finally:
        agent_tools_module.AgentLoop.__init__ = original_init  # type: ignore[method-assign]

    return captured_registries[0]


@pytest.mark.asyncio
async def test_write_documentation_is_restricted_to_its_documented_allowed_set(tmp_path: Path):
    sub_registry = await _captured_sub_registry(
        tmp_path, WRITE_DOCUMENTATION, {"query": "document the new feature"}
    )
    for name in ("read_file", "list_dir", "glob_search", "grep", "write_file", "edit_file"):
        assert name in sub_registry
    for name in ("run_shell", "call_python", "web_search", "web_fetch", "spawn_subagent"):
        assert name not in sub_registry


@pytest.mark.asyncio
async def test_verify_computation_is_restricted_to_its_documented_allowed_set(tmp_path: Path):
    sub_registry = await _captured_sub_registry(
        tmp_path, VERIFY_COMPUTATION, {"query": "check this sum"}
    )
    for name in ("read_file", "write_file", "call_python", "run_shell"):
        assert name in sub_registry
    for name in ("edit_file", "web_search", "web_fetch", "spawn_subagent"):
        assert name not in sub_registry


@pytest.mark.asyncio
async def test_deep_research_is_restricted_to_its_documented_allowed_set(tmp_path: Path):
    sub_registry = await _captured_sub_registry(tmp_path, DEEP_RESEARCH, {"query": "investigate this"})
    for name in (
        "read_file",
        "list_dir",
        "glob_search",
        "grep",
        "search_python",
        "inspect_python_module",
        "run_shell",
        "web_search",
        "web_fetch",
    ):
        assert name in sub_registry
    for name in ("write_file", "edit_file", "spawn_subagent"):
        assert name not in sub_registry


@pytest.mark.asyncio
async def test_data_analysis_is_restricted_to_its_documented_allowed_set(tmp_path: Path):
    sub_registry = await _captured_sub_registry(
        tmp_path, DATA_ANALYSIS, {"query": "analyze this dataset"}
    )
    for name in ("read_file", "list_dir", "glob_search", "call_python", "run_shell", "write_file"):
        assert name in sub_registry
    for name in ("edit_file", "web_search", "web_fetch", "spawn_subagent"):
        assert name not in sub_registry


def test_new_agent_tools_are_present_in_build_default_registry():
    registry = build_default_registry()
    assert "write_documentation" in registry
    assert "verify_computation" in registry
    assert "deep_research" in registry
    assert "data_analysis" in registry


def test_new_agent_tools_plan_mode_safety_matches_their_primary_purpose():
    # deep_research is primarily investigation and degrades gracefully
    # (loses run_shell) rather than becoming useless while plan mode
    # restricts write/execute tools — see make_agent_tool's _allowed(),
    # which filters per-tool regardless of the wrapper's own flag.
    assert DEEP_RESEARCH.plan_mode_safe is True
    # These three need write_file/run_shell/call_python to do anything
    # useful at all, so they're unavailable during plan mode entirely,
    # same as write_file/edit_file/run_shell themselves.
    assert WRITE_DOCUMENTATION.plan_mode_safe is False
    assert VERIFY_COMPUTATION.plan_mode_safe is False
    assert DATA_ANALYSIS.plan_mode_safe is False
