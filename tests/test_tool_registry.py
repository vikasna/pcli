"""Coverage for ToolRegistry.filtered: the shared subsetting primitive
behind spawn_subagent's allowed_tools, plan mode's tool filtering, and
agent tools' fixed allowed-tool sets."""

from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.registry import ToolRegistry


async def _noop_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="ok")


def _tool(name: str, *, plan_mode_safe: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        handler=_noop_handler,
        needs_permission=False,
        plan_mode_safe=plan_mode_safe,
    )


def test_filtered_returns_a_new_registry_containing_only_matching_tools():
    registry = ToolRegistry()
    registry.register(_tool("a", plan_mode_safe=True))
    registry.register(_tool("b", plan_mode_safe=False))
    registry.register(_tool("c", plan_mode_safe=True))

    subset = registry.filtered(lambda t: t.plan_mode_safe)

    assert {t.name for t in subset} == {"a", "c"}
    assert "b" not in subset


def test_filtered_does_not_mutate_the_original_registry():
    registry = ToolRegistry()
    registry.register(_tool("a", plan_mode_safe=True))
    registry.register(_tool("b", plan_mode_safe=False))

    registry.filtered(lambda t: t.plan_mode_safe)

    assert len(registry) == 2
    assert "a" in registry
    assert "b" in registry


def test_filtered_with_predicate_matching_nothing_returns_empty_registry():
    registry = ToolRegistry()
    registry.register(_tool("a"))

    subset = registry.filtered(lambda t: False)

    assert len(subset) == 0


# --- build_default_registry: read_only classification ---
#
# read_only is deliberately NOT derived from needs_permission or
# plan_mode_safe (see ToolSpec.read_only's own docstring) - these
# spot-checks specifically cover the tools where read_only disagrees with
# one or both of those other two flags, the cases most likely to silently
# regress to the wrong value later.


def test_build_default_registry_gives_every_tool_a_bool_read_only_flag():
    from pcli.tools.registry import build_default_registry

    registry = build_default_registry()
    assert len(registry) > 0
    for tool in registry:
        assert isinstance(tool.read_only, bool), tool.name


def test_read_only_classification_spot_checks():
    from pcli.tools.registry import build_default_registry

    registry = build_default_registry()
    by_name = {t.name: t for t in registry}

    assert by_name["read_file"].read_only is True

    # needs_permission=False but NOT read-only - the exact case
    # ToolSpec.plan_mode_safe's own docstring warns about.
    assert by_name["write_todos"].needs_permission is False
    assert by_name["write_todos"].read_only is False
    assert by_name["record_decision"].needs_permission is False
    assert by_name["record_decision"].read_only is False

    # plan_mode_safe=True but NOT read-only - these can run run_shell (via
    # their own allowed-tool set / delegation) outside of plan mode.
    assert by_name["spawn_subagent"].plan_mode_safe is True
    assert by_name["spawn_subagent"].read_only is False
    assert by_name["deep_research"].plan_mode_safe is True
    assert by_name["deep_research"].read_only is False

    # A genuinely read-only agent persona, for contrast.
    assert by_name["explore_codebase"].read_only is True
