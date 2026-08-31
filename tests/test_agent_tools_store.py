"""Coverage for agent_tools_store.py: persistence for user/model-registered
agent tools, and reloading them into a working ToolRegistry (the mechanism
ChatScreen.on_mount uses at startup, mirroring toolbox tools)."""

from pcli.config.paths import agent_tools_file
from pcli.tools.agent_tools_store import (
    load_persisted_agent_tools,
    read_agent_tools,
    save_agent_tool,
)


def test_save_agent_tool_persists_to_disk():
    save_agent_tool(
        "my_tool",
        description="desc",
        persona_prompt="persona",
        allowed_tools=["read_file"],
        plan_mode_safe=True,
    )

    assert agent_tools_file().exists()
    stored = read_agent_tools()
    assert stored["my_tool"] == {
        "description": "desc",
        "persona_prompt": "persona",
        "allowed_tools": ["read_file"],
        "plan_mode_safe": True,
    }


def test_read_agent_tools_with_nothing_saved_returns_empty_dict():
    assert read_agent_tools() == {}


def test_save_agent_tool_overwrites_an_existing_entry_with_the_same_name():
    save_agent_tool(
        "my_tool", description="v1", persona_prompt="p1", allowed_tools=["read_file"], plan_mode_safe=False
    )
    save_agent_tool(
        "my_tool", description="v2", persona_prompt="p2", allowed_tools=["grep"], plan_mode_safe=True
    )

    stored = read_agent_tools()
    assert len(stored) == 1
    assert stored["my_tool"]["description"] == "v2"


def test_load_persisted_agent_tools_rebuilds_a_working_registry():
    save_agent_tool(
        "explorer_a",
        description="desc a",
        persona_prompt="persona a",
        allowed_tools=["read_file"],
        plan_mode_safe=True,
    )
    save_agent_tool(
        "explorer_b",
        description="desc b",
        persona_prompt="persona b",
        allowed_tools=["grep"],
        plan_mode_safe=False,
    )

    registry = load_persisted_agent_tools()

    assert "explorer_a" in registry
    assert "explorer_b" in registry
    tool_a = registry.get("explorer_a")
    assert tool_a.description == "desc a"
    assert tool_a.plan_mode_safe is True
    tool_b = registry.get("explorer_b")
    assert tool_b.plan_mode_safe is False


def test_load_persisted_agent_tools_with_nothing_saved_returns_empty_registry():
    registry = load_persisted_agent_tools()
    assert len(registry) == 0
