"""Coverage for describe_tool - lets the model ask for full detail (and
curated example calls) about any tool in its current tool set. See
fs_tools.py's edit_file for the real confusion that motivated this: a
one-line tool summary alone wasn't enough for some models to discover
edit_file's insert mode."""

from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.builtin.describe_tool import _EXAMPLES, DESCRIBE_TOOL
from pcli.tools.builtin.fs_tools import EDIT_FILE, READ_FILE, WRITE_FILE
from pcli.tools.registry import ToolRegistry, build_default_registry


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("describe_tool should never touch the sandbox")


async def _no_examples_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="n/a")


_NO_EXAMPLES_TOOL = ToolSpec(
    name="a_tool_with_no_curated_examples",
    description="A synthetic tool used only to test describe_tool's fallback text.",
    parameters={"type": "object", "properties": {}},
    handler=_no_examples_handler,
    needs_permission=False,
)


def _ctx(cwd: Path, registry: ToolRegistry | None = None) -> ToolContext:
    return ToolContext(
        sandbox=_NullSandbox(),
        guardrails=GuardrailsConfig(),
        cwd=cwd,
        tool_registry=registry,
    )


def _small_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (READ_FILE, WRITE_FILE, EDIT_FILE, _NO_EXAMPLES_TOOL):
        registry.register(tool)
    return registry


@pytest.mark.asyncio
async def test_describes_a_simple_read_only_tool(tmp_path: Path):
    ctx = _ctx(tmp_path, _small_registry())
    result = await DESCRIBE_TOOL.handler({"name": "read_file"}, ctx)
    assert result.is_error is False
    assert "# read_file" in result.output
    assert READ_FILE.description in result.output
    assert "Needs permission: False" in result.output
    assert "Available in plan mode: True" in result.output
    assert '"path"' in result.output  # parameter schema included
    assert "## Example calls" in result.output
    assert "src/app.py" in result.output  # from the curated example


@pytest.mark.asyncio
async def test_describes_a_permission_gated_tool_with_its_risk_description(tmp_path: Path):
    ctx = _ctx(tmp_path, _small_registry())
    result = await DESCRIBE_TOOL.handler({"name": "write_file"}, ctx)
    assert result.is_error is False
    assert f"Needs permission: True — {WRITE_FILE.risk_description}" in result.output


@pytest.mark.asyncio
async def test_describes_edit_file_with_all_three_modes_as_separate_examples(tmp_path: Path):
    """The concrete motivating case: edit_file has multiple modes, so it
    should show multiple, clearly distinct example calls."""
    ctx = _ctx(tmp_path, _small_registry())
    result = await DESCRIBE_TOOL.handler({"name": "edit_file"}, ctx)
    assert result.is_error is False
    assert "old_string" in result.output and "new_string" in result.output
    assert "insert_after_line" in result.output
    assert "delete_start_line" in result.output
    assert "replace_all" in result.output
    # At least the five curated edit_file examples, each numbered.
    for i in range(1, 6):
        assert f"{i}. edit_file(" in result.output


@pytest.mark.asyncio
async def test_falls_back_to_a_generic_note_when_no_examples_are_curated(tmp_path: Path):
    ctx = _ctx(tmp_path, _small_registry())
    result = await DESCRIBE_TOOL.handler({"name": "a_tool_with_no_curated_examples"}, ctx)
    assert result.is_error is False
    assert "No curated examples for this tool yet" in result.output


@pytest.mark.asyncio
async def test_unknown_tool_name_lists_available_tools(tmp_path: Path):
    ctx = _ctx(tmp_path, _small_registry())
    result = await DESCRIBE_TOOL.handler({"name": "nonexistent_tool"}, ctx)
    assert result.is_error is True
    assert "No tool named 'nonexistent_tool'" in result.output
    assert "[pcli] Suggestion:" in result.output
    assert "read_file" in result.output  # the available-tools list


@pytest.mark.asyncio
async def test_no_tool_registry_in_context_errors_cleanly(tmp_path: Path):
    ctx = _ctx(tmp_path, registry=None)
    result = await DESCRIBE_TOOL.handler({"name": "read_file"}, ctx)
    assert result.is_error is True
    assert "No tool registry available" in result.output


def test_every_registered_tool_has_a_curated_example_except_describe_tool_itself():
    """Regression guard: a new builtin tool added later without a curated
    example would otherwise silently fall back to the generic note - not
    wrong, but worth catching so examples get added deliberately, not by
    omission. describe_tool itself is the one legitimate exception (no
    useful self-referential example)."""
    registry = build_default_registry()
    names = {tool.name for tool in registry}
    missing = names - set(_EXAMPLES.keys()) - {DESCRIBE_TOOL.name}
    assert missing == set()


def test_describe_tool_is_registered_by_default():
    registry = build_default_registry()
    assert "describe_tool" in registry


def test_describe_tool_itself_is_read_only_and_plan_mode_safe():
    assert DESCRIBE_TOOL.needs_permission is False
    assert DESCRIBE_TOOL.plan_mode_safe is True
