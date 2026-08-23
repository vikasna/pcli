"""register_toolbox_tool: lets the LLM itself register a callable tool via
the toolbox mechanism (tools/toolbox/manager.py) instead of that being a
human-only, slash-command-triggered action. Aimed at the case where the
model has just written its own small reusable script (e.g. for something it
expects to repeat) and wants a real tool wrapping it, rather than re-deriving
the same shell incantation via run_shell every time.
"""

from __future__ import annotations

from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.toolbox.manager import ToolboxDiscoveryError


async def _register_toolbox_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.toolbox_manager is None:
        return ToolResult(output="Toolbox isn't available.", is_error=True)

    name = arguments["name"]
    path = arguments.get("path")
    try:
        summary = await ctx.toolbox_manager.discover(
            name, gateway_client=ctx.gateway_client, model=ctx.model, path=path
        )
    except ToolboxDiscoveryError as exc:
        message = str(exc)
        if path is not None and ("doesn't exist" in message or "was not found" in message):
            suggestion = "write_file the script first, or double-check the path is correct."
        elif path is None:
            suggestion = (
                "if this is your own script rather than an installed CLI, pass path= instead "
                "of relying on PATH lookup."
            )
        else:
            suggestion = "make sure the script has a working --help that prints usage text."
        return ToolResult(output=f"{message}\n[pcli] Suggestion: {suggestion}", is_error=True)

    if ctx.tool_registry is not None:
        loaded = await ctx.toolbox_manager.load_all()
        ctx.tool_registry.merge(loaded)

    return ToolResult(output=summary)


REGISTER_TOOLBOX_TOOL = ToolSpec(
    name="register_toolbox_tool",
    description="Registers a new callable tool from a script's --help output — either an "
    "already-installed CLI on PATH (name only), or your own self-authored script (name + "
    "path, e.g. one you just wrote with write_file and a working --help/argparse). Useful "
    "when a task needs the same multi-step shell incantation repeatedly: write a small "
    "script once, register it here, and call the resulting tool directly next time instead "
    "of re-deriving the shell command. This is a judgment call for genuinely repetitive "
    "work, not every one-off command — and it's permission-gated, so it isn't free.",
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short identifier for the new tool group (used as a prefix, "
                "e.g. 'name_subcommand').",
            },
            "path": {
                "type": "string",
                "description": "Path to a self-authored script (relative to the working "
                "directory, or absolute within it). Omit to look the name up on PATH "
                "instead (for an already-installed CLI).",
            },
        },
        "required": ["name"],
    },
    handler=_register_toolbox_tool,
    needs_permission=True,
    risk_description="Registers a new tool the model can call in later turns — a bigger "
    "action than running one command, since it grants standing execution rights.",
)
