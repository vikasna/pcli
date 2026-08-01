import pytest

import pcli.tools.toolbox.manager as manager_module
import pcli.tools.toolbox.store as store_module
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.toolbox.manager import (
    ToolboxDiscoveryError,
    ToolboxManager,
    _auto_flags,
    make_command_tool_spec,
    make_synthesized_tool_spec,
)
from pcli.tools.toolbox.plugin_base import CommandSpec, DetectionResult, ToolboxPlugin


@pytest.fixture(autouse=True)
def _isolated_toolbox_dir(tmp_path, monkeypatch):
    toolbox_root = tmp_path / "toolbox"
    monkeypatch.setattr(store_module, "toolbox_dir", lambda: toolbox_root)
    return toolbox_root


class _FakeDetectSandbox:
    def __init__(self, output: str = "widget version 1.2.3") -> None:
        self.output = output
        self.calls: list[ExecRequest] = []

    async def execute(self, request: ExecRequest) -> ExecResult:
        self.calls.append(request)
        return ExecResult(
            stdout=self.output, stderr="", exit_code=0, timed_out=False, backend_used="fake"
        )


class FakeWidgetPlugin(ToolboxPlugin):
    software_name = "widget"
    binary_names = ("widget",)
    version_args = ("--version",)

    def parse_version(self, version_output: str):
        return (1, 2, 3)

    def build_tools(self, detection: DetectionResult) -> list[CommandSpec]:
        return [
            CommandSpec(
                name="status",
                description="Show widget status.",
                parameters={"type": "object", "properties": {}},
                build_args=lambda args: ["status"],
                binary_path=detection.binary_path,
                risk="read",
            ),
            CommandSpec(
                name="reset",
                description="Reset widget state.",
                parameters={"type": "object", "properties": {}},
                build_args=lambda args: ["reset"],
                binary_path=detection.binary_path,
                risk="destructive",
            ),
        ]


class _RunSandbox(Sandbox):
    name = "run-fake"

    def __init__(self, result: ExecResult) -> None:
        self._result = result
        self.calls: list[ExecRequest] = []

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        self.calls.append(request)
        return self._result


def test_store_registry_roundtrip():
    assert store_module.read_registry() == {}
    registry = {
        "widget": {
            "source": "curated",
            "binary_path": "/usr/bin/widget",
            "version": "1.2.3",
            "tool_count": 2,
        }
    }
    store_module.write_registry(registry)
    assert store_module.read_registry() == registry


def test_store_synthesized_schema_roundtrip():
    assert store_module.read_synthesized_schema("widget") is None
    store_module.write_synthesized_schema(
        "widget", help_corpus_hash="abc123", version="unknown", tools=[{"name": "list"}]
    )
    cached = store_module.read_synthesized_schema("widget")
    assert cached["help_corpus_hash"] == "abc123"
    assert cached["tools"] == [{"name": "list"}]


def test_hash_corpus_deterministic_and_sensitive_to_content():
    assert store_module.hash_corpus("abc") == store_module.hash_corpus("abc")
    assert store_module.hash_corpus("abc") != store_module.hash_corpus("abcd")


@pytest.mark.asyncio
async def test_discover_curated_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_module, "ALL_PLUGINS", [FakeWidgetPlugin()])
    monkeypatch.setattr(
        manager_module.shutil, "which", lambda name: "/usr/bin/widget" if name == "widget" else None
    )
    fake_sandbox = _FakeDetectSandbox("widget version 1.2.3")
    monkeypatch.setattr(manager_module, "RestrictedSubprocessSandbox", lambda **kw: fake_sandbox)

    manager = ToolboxManager(cwd=tmp_path)
    summary = await manager.discover("widget")
    assert "Registered 2 tool(s)" in summary

    registry = store_module.read_registry()
    assert registry["widget"]["source"] == "curated"
    assert registry["widget"]["tool_count"] == 2


@pytest.mark.asyncio
async def test_discover_unknown_binary_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_module, "ALL_PLUGINS", [])
    monkeypatch.setattr(manager_module.shutil, "which", lambda name: None)
    manager = ToolboxManager(cwd=tmp_path)
    with pytest.raises(ToolboxDiscoveryError):
        await manager.discover("totally-unknown-tool-xyz")


@pytest.mark.asyncio
async def test_load_all_rebuilds_curated_tools_and_skips_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_module, "ALL_PLUGINS", [FakeWidgetPlugin()])
    monkeypatch.setattr(
        manager_module.shutil, "which", lambda name: "/usr/bin/widget" if name == "widget" else None
    )
    fake_sandbox = _FakeDetectSandbox("widget version 1.2.3")
    monkeypatch.setattr(manager_module, "RestrictedSubprocessSandbox", lambda **kw: fake_sandbox)

    manager = ToolboxManager(cwd=tmp_path)
    await manager.discover("widget")

    # A stale registry entry for software that's no longer on PATH must be skipped, not crash.
    registry = store_module.read_registry()
    registry["ghost-tool"] = {
        "source": "curated",
        "binary_path": "/nowhere",
        "version": "?",
        "tool_count": 0,
    }
    store_module.write_registry(registry)

    loaded = await manager.load_all()
    names = {t.name for t in loaded}
    assert names == {"widget_status", "widget_reset"}


@pytest.mark.asyncio
async def test_make_command_tool_spec_executes_through_sandbox(tmp_path):
    spec = CommandSpec(
        name="status",
        description="Show status.",
        parameters={"type": "object", "properties": {}},
        build_args=lambda args: ["status"],
        binary_path="/usr/bin/widget",
        risk="read",
    )
    tool_spec = make_command_tool_spec("widget", spec)
    assert tool_spec.name == "widget_status"
    assert tool_spec.needs_permission is False  # risk == "read"

    sandbox = _RunSandbox(
        ExecResult(stdout="ok", stderr="", exit_code=0, timed_out=False, backend_used="run-fake")
    )
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await tool_spec.handler({}, ctx)
    assert result.is_error is False
    assert "ok" in result.output
    assert sandbox.calls[0].command == ["/usr/bin/widget", "status"]


def test_auto_flags_generic_conversion():
    parameters = {
        "type": "object",
        "properties": {
            "verbose": {"type": "boolean"},
            "count": {"type": "integer"},
            "tags": {"type": "array"},
        },
    }
    argv = _auto_flags({"verbose": True, "count": 3, "tags": ["a", "b"], "unset": None}, parameters)
    assert argv == ["--verbose", "--count", "3", "--tags", "a", "--tags", "b"]


@pytest.mark.asyncio
async def test_make_synthesized_tool_spec_executes_through_sandbox(tmp_path):
    tool_data = {
        "name": "list",
        "description": "List things.",
        "subcommand": ["list"],
        "parameters": {"type": "object", "properties": {"all": {"type": "boolean"}}},
        "risk": "mutate",
    }
    tool_spec = make_synthesized_tool_spec("widget", "/usr/bin/widget", tool_data)
    assert tool_spec.name == "widget_list"
    assert tool_spec.needs_permission is True

    sandbox = _RunSandbox(
        ExecResult(stdout="listed", stderr="", exit_code=0, timed_out=False, backend_used="run-fake")
    )
    ctx = ToolContext(sandbox=sandbox, guardrails=GuardrailsConfig(), cwd=tmp_path)
    result = await tool_spec.handler({"all": True}, ctx)
    assert "listed" in result.output
    assert sandbox.calls[0].command == ["/usr/bin/widget", "list", "--all"]
