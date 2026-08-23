"""Coverage for self-authored toolbox tools: ToolboxManager.discover's path=
parameter (bypassing PATH lookup, for a model-written script), the
invocation: list[str] argv-prefix it produces for .py scripts, path
containment, and registry.json's backward-compatible fallback for entries
written before the invocation field existed.

Also covers register_toolbox_tool (tools/builtin/toolbox_register_tool.py),
the new tool that lets the model trigger this itself rather than it being
slash-command/CLI-only.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

import pcli.tools.toolbox.store as store_module
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext
from pcli.tools.builtin.toolbox_register_tool import REGISTER_TOOLBOX_TOOL
from pcli.tools.registry import ToolRegistry
from pcli.tools.toolbox.manager import ToolboxDiscoveryError, ToolboxManager


@pytest.fixture(autouse=True)
def _isolated_toolbox_dir(tmp_path, monkeypatch):
    toolbox_root = tmp_path / "toolbox_data"
    monkeypatch.setattr(store_module, "toolbox_dir", lambda: toolbox_root)
    return toolbox_root


_SCRIPT = """\
import argparse

parser = argparse.ArgumentParser(prog="mytool")
sub = parser.add_subparsers(dest="command")
greet = sub.add_parser("greet")
greet.add_argument("--name")
args = parser.parse_args()
if args.command == "greet":
    print(f"hello {args.name}")
"""


def _write_script(tmp_path: Path, name: str = "mytool.py") -> Path:
    script = tmp_path / name
    script.write_text(_SCRIPT, encoding="utf-8")
    return script


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="k",
        default_model="fake-model",
        max_retries=1,
    )


def _sse(text: str) -> bytes:
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    return (f"data: {json.dumps(chunk)}\n\n" + "data: [DONE]\n\n").encode()


_SYNTHESIZED = {
    "tools": [
        {
            "name": "greet",
            "description": "Greet someone.",
            "subcommand": ["greet"],
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
            "risk": "read",
        }
    ]
}


@pytest.mark.asyncio
@respx.mock
async def test_discover_with_path_bypasses_path_lookup_and_registers_invocation(tmp_path: Path):
    script = _write_script(tmp_path)
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(json.dumps(_SYNTHESIZED)))
    )

    manager = ToolboxManager(cwd=tmp_path)
    async with GatewayClient(_settings()) as client:
        summary = await manager.discover("mytool", gateway_client=client, path="mytool.py")

    assert "Registered 'mytool'" in summary
    registry = store_module.read_registry()
    assert registry["mytool"]["invocation"] == [sys.executable, str(script)]
    assert registry["mytool"]["source"] == "synthesized"


@pytest.mark.asyncio
async def test_discover_with_path_rejects_paths_outside_the_working_directory(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    outside_script = tmp_path / "outside.py"
    outside_script.write_text(_SCRIPT, encoding="utf-8")

    manager = ToolboxManager(cwd=project)
    with pytest.raises(SandboxSecurityError):
        await manager.discover("evil", path="../outside.py")


@pytest.mark.asyncio
async def test_discover_with_path_rejects_a_missing_file(tmp_path: Path):
    manager = ToolboxManager(cwd=tmp_path)
    with pytest.raises(ToolboxDiscoveryError):
        await manager.discover("mytool", path="does-not-exist.py")


@pytest.mark.asyncio
@respx.mock
async def test_registered_synthesized_tool_actually_runs_the_script(tmp_path: Path):
    _write_script(tmp_path)
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(json.dumps(_SYNTHESIZED)))
    )

    manager = ToolboxManager(cwd=tmp_path)
    async with GatewayClient(_settings()) as client:
        await manager.discover("mytool", gateway_client=client, path="mytool.py")

    loaded = await manager.load_all()
    tool = loaded.get("mytool_greet")
    assert tool is not None

    sandbox = RestrictedSubprocessSandbox(allowed_roots=[tmp_path])
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await tool.handler({"name": "world"}, ctx)
    assert result.is_error is False
    assert "hello world" in result.output


@pytest.mark.asyncio
async def test_load_all_falls_back_to_binary_path_for_pre_invocation_registry_entries(tmp_path: Path):
    """Registry entries written before the invocation field existed only
    have binary_path - load_all must still work for them."""
    script = _write_script(tmp_path, "legacy.py")
    store_module.write_synthesized_schema(
        "legacy", help_corpus_hash="whatever", version="unknown", tools=_SYNTHESIZED["tools"]
    )
    store_module.write_registry(
        {
            "legacy": {
                "source": "synthesized",
                "binary_path": str(script),
                # no "invocation" key - simulates a pre-upgrade entry
                "version": "unknown",
                "tool_count": 1,
            }
        }
    )

    manager = ToolboxManager(cwd=tmp_path)
    loaded = await manager.load_all()
    # Falls back to [binary_path] directly - won't actually run (no
    # interpreter prefix for a .py file), but must not crash load_all.
    assert loaded.get("legacy_greet") is not None


# --- register_toolbox_tool ---


def _ctx_with_toolbox(tmp_path: Path, manager: ToolboxManager, registry: ToolRegistry) -> ToolContext:
    from pcli.sandbox.null_backend import NullSandbox

    return ToolContext(
        sandbox=NullSandbox(),
        guardrails=GuardrailsConfig(),
        cwd=tmp_path,
        tool_registry=registry,
        toolbox_manager=manager,
    )


def test_register_toolbox_tool_always_needs_permission():
    """Registering a new callable tool is a bigger action than running one
    command - always permission-gated regardless of the synthesized tool's
    own eventual risk level."""
    assert REGISTER_TOOLBOX_TOOL.needs_permission is True


@pytest.mark.asyncio
@respx.mock
async def test_register_toolbox_tool_registers_the_new_tool_into_the_registry(tmp_path: Path):
    _write_script(tmp_path)
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(json.dumps(_SYNTHESIZED)))
    )

    manager = ToolboxManager(cwd=tmp_path)
    registry = ToolRegistry()
    async with GatewayClient(_settings()) as client:
        ctx = _ctx_with_toolbox(tmp_path, manager, registry)
        ctx.gateway_client = client
        result = await REGISTER_TOOLBOX_TOOL.handler({"name": "mytool", "path": "mytool.py"}, ctx)

    assert result.is_error is False
    assert registry.get("mytool_greet") is not None


@pytest.mark.asyncio
async def test_register_toolbox_tool_without_toolbox_manager_is_a_clean_error(tmp_path: Path):
    from pcli.sandbox.null_backend import NullSandbox

    ctx = ToolContext(sandbox=NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await REGISTER_TOOLBOX_TOOL.handler({"name": "mytool"}, ctx)
    assert result.is_error is True


@pytest.mark.asyncio
async def test_register_toolbox_tool_missing_script_suggests_write_file(tmp_path: Path):
    manager = ToolboxManager(cwd=tmp_path)
    registry = ToolRegistry()
    ctx = _ctx_with_toolbox(tmp_path, manager, registry)
    result = await REGISTER_TOOLBOX_TOOL.handler({"name": "mytool", "path": "nope.py"}, ctx)
    assert result.is_error is True
    assert "[pcli] Suggestion:" in result.output
    assert "write_file" in result.output


