"""Persistence for user/model-registered agent tools (see
tools/agent_tools.py's make_agent_tool and
tools/builtin/agent_tool_register_tool.py), mirroring tools/toolbox/store.py's
shape but keyed by tool name rather than software name."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pcli.config.paths import agent_tools_file


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def read_agent_tools() -> dict[str, dict]:
    path = agent_tools_file()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_agent_tools(agent_tools: dict[str, dict]) -> None:
    _atomic_write(agent_tools_file(), json.dumps(agent_tools, indent=2))


def save_agent_tool(
    name: str,
    *,
    description: str,
    persona_prompt: str,
    allowed_tools: list[str],
    plan_mode_safe: bool,
) -> None:
    agent_tools = read_agent_tools()
    agent_tools[name] = {
        "description": description,
        "persona_prompt": persona_prompt,
        "allowed_tools": allowed_tools,
        "plan_mode_safe": plan_mode_safe,
    }
    write_agent_tools(agent_tools)


def load_persisted_agent_tools():
    """Builds a ToolRegistry from every agent tool saved via save_agent_tool
    — called once at startup (ChatScreen.on_mount) so they survive restarts,
    same as toolbox tools."""
    from pcli.tools.agent_tools import make_agent_tool
    from pcli.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for name, spec in read_agent_tools().items():
        registry.register(
            make_agent_tool(
                name=name,
                description=spec["description"],
                persona_prompt=spec["persona_prompt"],
                allowed_tool_names=spec["allowed_tools"],
                plan_mode_safe=spec.get("plan_mode_safe", False),
            )
        )
    return registry
