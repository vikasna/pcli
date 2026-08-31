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
