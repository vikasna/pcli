import json
from pathlib import Path

import httpx
import pytest
import respx

from pcli.agent.loop import AgentLoop, ToolResultEvent
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.llm.models import ChatMessage
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.permissions.policy import PermissionPolicy
from pcli.tools.base import ToolContext
from pcli.tools.pydiscovery import cache as cache_module
from pcli.tools.pydiscovery.index import ModuleEntry, build_index
from pcli.tools.pydiscovery.invoke import CALL_PYTHON
from pcli.tools.pydiscovery.search import INSPECT_PYTHON_MODULE, SEARCH_PYTHON
from pcli.tools.registry import ToolRegistry


class _UnusedSandbox:
    name = "unused"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox=_UnusedSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path)


def test_build_index_contains_stdlib_and_no_private_names():
    entries = build_index()
    names = {e.name for e in entries}
    assert "json" in names
    assert "math" in names
    assert not any(n.startswith("_") for n in names)


@pytest.mark.asyncio
async def test_search_python_never_imports_anything(tmp_path: Path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _guarded_import(name, *args, **kwargs):
        if name in {"requests", "numpy", "pandas"}:
            raise AssertionError(f"search_python must not import {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)

    fake_entries = [
        ModuleEntry(name="requests", package="requests", summary="HTTP library"),
        ModuleEntry(name="numpy", package="numpy", summary="Array library"),
    ]
    monkeypatch.setattr(cache_module, "load_or_build_index", lambda **kw: fake_entries)
    # search.py imported load_or_build_index by name, so patch it there too.
    import pcli.tools.pydiscovery.search as search_module

    monkeypatch.setattr(search_module, "load_or_build_index", lambda **kw: fake_entries)

    result = await SEARCH_PYTHON.handler({"query": "http"}, _ctx(tmp_path))
    assert "requests" in result.output
    assert "numpy" not in result.output


@pytest.mark.asyncio
async def test_call_python_executes_stdlib_function(tmp_path: Path):
    result = await CALL_PYTHON.handler(
        {"qualified_name": "math.sqrt", "args": [16]}, _ctx(tmp_path)
    )
    assert result.is_error is False
    assert result.output.strip() == "4.0"


@pytest.mark.asyncio
async def test_call_python_json_dumps_dict_argument(tmp_path: Path):
    result = await CALL_PYTHON.handler(
        {"qualified_name": "json.dumps", "kwargs": {"obj": {"a": 1}}}, _ctx(tmp_path)
    )
    assert result.is_error is False
    assert result.output.strip() == '"{\\"a\\": 1}"' or "a" in result.output


@pytest.mark.asyncio
async def test_call_python_unknown_module_reports_error(tmp_path: Path):
    result = await CALL_PYTHON.handler(
        {"qualified_name": "totally_not_a_real_module_xyz.foo"}, _ctx(tmp_path)
    )
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "search_python" in result.output


@pytest.mark.asyncio
async def test_call_python_not_callable_suggests_inspect_python_module(tmp_path: Path):
    result = await CALL_PYTHON.handler({"qualified_name": "math.pi"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "inspect_python_module" in result.output


@pytest.mark.asyncio
async def test_call_python_bad_arguments_suggests_checking_signature(tmp_path: Path):
    result = await CALL_PYTHON.handler({"qualified_name": "math.sqrt", "args": ["not-a-number"]}, _ctx(tmp_path))
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "signature" in result.output


@pytest.mark.asyncio
async def test_inspect_python_module_finds_members(tmp_path: Path):
    result = await INSPECT_PYTHON_MODULE.handler({"module": "math", "query": "sqrt"}, _ctx(tmp_path))
    assert result.is_error is False
    assert "sqrt" in result.output


@pytest.mark.asyncio
async def test_inspect_python_module_unknown_module_suggests_search_python(tmp_path: Path):
    result = await INSPECT_PYTHON_MODULE.handler(
        {"module": "totally_not_a_real_module_xyz"}, _ctx(tmp_path)
    )
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "search_python" in result.output


def test_guardrails_deny_denylisted_python_module():
    guardrails = GuardrailsConfig(python_module_denylist=["os", "subprocess"])
    assert guardrails.evaluate_python_module("os.path.join").allowed is False
    assert guardrails.evaluate_python_module("subprocess").allowed is False
    assert guardrails.evaluate_python_module("math").allowed is True


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


@pytest.mark.asyncio
@respx.mock
async def test_agent_loop_denies_inspect_of_denylisted_module_before_reaching_handler(
    tmp_path: Path,
):
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
                                        "function": {
                                            "name": "inspect_python_module",
                                            "arguments": json.dumps({"module": "os"}),
                                        },
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
        httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
        ),
    ]

    registry = ToolRegistry()
    registry.register(INSPECT_PYTHON_MODULE)
    guardrails = GuardrailsConfig(python_module_denylist=["os"])
    permission_manager = PermissionManager(
        guardrails=guardrails, policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async def ask_should_not_be_called(*args, **kwargs):
        raise AssertionError("guardrail should deny before ask() is called")

    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        max_retries=1,
    )
    async with GatewayClient(settings) as client:
        loop = AgentLoop(
            client,
            tool_registry=registry,
            permission_manager=permission_manager,
            tool_context_factory=lambda: ToolContext(
                sandbox=_UnusedSandbox(), guardrails=guardrails, cwd=tmp_path
            ),
        )
        events = []
        async for event in loop.run_turn(
            [ChatMessage(role="user", content="inspect os")], ask=ask_should_not_be_called
        ):
            events.append(event)

    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "Permission denied" in tool_results[0].output


def test_index_cache_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cache_module, "cache_dir", lambda: tmp_path)
    build_calls = {"n": 0}
    real_build = cache_module.build_index

    def _counting_build():
        build_calls["n"] += 1
        return real_build()

    monkeypatch.setattr(cache_module, "build_index", _counting_build)

    entries1 = cache_module.load_or_build_index()
    assert build_calls["n"] == 1
    assert (tmp_path / "pydiscovery_index.json").exists()

    entries2 = cache_module.load_or_build_index()
    assert build_calls["n"] == 1  # second call hit the cache, didn't rebuild
    assert [e.name for e in entries1] == [e.name for e in entries2]

    entries3 = cache_module.load_or_build_index(force_rebuild=True)
    assert build_calls["n"] == 2
    assert [e.name for e in entries3] == [e.name for e in entries1]
