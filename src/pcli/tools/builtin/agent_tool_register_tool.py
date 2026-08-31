"""register_agent_tool: lets the LLM define a new named subagent persona,
restricted to a fixed set of already-existing tools, callable afterward as
a single tool with one 'query' argument — the same underlying mechanism as
spawn_subagent (tools/agent_tools.py's make_agent_tool), but with the
persona/allowed-tools baked in at registration time instead of supplied by
the calling model on every call. Persisted (tools/agent_tools_store.py) so
it survives restarts, mirroring register_toolbox_tool but for subagent
personas instead of external scripts.
"""

from __future__ import annotations

from pcli.tools.agent_tools import make_agent_tool
from pcli.tools.agent_tools_store import save_agent_tool
from pcli.tools.base import ToolContext, ToolResult, ToolSpec


async def _register_agent_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.tool_registry is None:
        return ToolResult(output="Agent tools aren't available in this context.", is_error=True)

    name = arguments["name"]
    description = arguments["description"]
    persona_prompt = arguments["persona_prompt"]
    allowed_tools = arguments["allowed_tools"]
    plan_mode_safe = arguments.get("plan_mode_safe", False)

    unknown = [t for t in allowed_tools if t not in ctx.tool_registry]
    if unknown:
        return ToolResult(
            output=f"Unknown tool name(s): {', '.join(unknown)}\n"
            "[pcli] Suggestion: only reference tools that already exist in your own tool set "
            "(check the exact names you were given).",
            is_error=True,
        )

    save_agent_tool(
        name,
        description=description,
        persona_prompt=persona_prompt,
        allowed_tools=allowed_tools,
        plan_mode_safe=plan_mode_safe,
    )
    tool = make_agent_tool(
        name=name,
        description=description,
        persona_prompt=persona_prompt,
        allowed_tool_names=allowed_tools,
        plan_mode_safe=plan_mode_safe,
    )
    ctx.tool_registry.register(tool)

    return ToolResult(output=f"Registered agent tool '{name}' — callable from now on.")


REGISTER_AGENT_TOOL = ToolSpec(
    name="register_agent_tool",
    description="Defines a new named subagent persona, callable afterward as a single tool "
    "with one 'query' argument. Use this when you find yourself wanting to delegate the same "
    "kind of focused sub-task repeatedly (a consistent persona and a fixed, restricted set of "
    "tools), rather than repeatedly spelling out the same instructions to spawn_subagent. "
    "Persists across restarts — this is a judgment call for genuinely repetitive delegation, "
    "not every one-off task.",
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Tool name the model will call it by afterward.",
            },
            "description": {
                "type": "string",
                "description": "Shown to the calling model as this tool's description.",
            },
            "persona_prompt": {
                "type": "string",
                "description": "System prompt for the nested subagent: its role/persona and "
                "how it should approach its task.",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fixed set of existing tool names the nested subagent is "
                "restricted to.",
            },
            "plan_mode_safe": {
                "type": "boolean",
                "description": "Whether this new tool should stay usable while plan mode is "
                "active (default false). Only set true if every tool in allowed_tools is "
                "itself read-only/exploration-only.",
            },
        },
        "required": ["name", "description", "persona_prompt", "allowed_tools"],
    },
    handler=_register_agent_tool,
    needs_permission=True,
    risk_description="Registers a new tool the model can call in later turns — grants standing "
    "execution rights to a nested subagent, a bigger action than running one command.",
)
