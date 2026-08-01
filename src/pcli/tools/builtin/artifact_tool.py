"""fetch_artifact: retrieves the full (or a windowed slice of the) content of
a large tool result that AgentLoop truncated out of the live conversation
and archived — see pcli.tools.artifacts and agent/loop.py's dispatch logic."""

from __future__ import annotations

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_DEFAULT_FETCH_CHARS = 4000


async def _fetch_artifact(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.artifact_store is None:
        return ToolResult(output="No artifact store available in this context.", is_error=True)

    artifact_id = arguments["artifact_id"]
    content = ctx.artifact_store.get(artifact_id)
    if content is None:
        return ToolResult(output=f"No artifact found with id '{artifact_id}'.", is_error=True)

    offset = max(0, int(arguments.get("offset") or 0))
    limit = int(arguments.get("limit") or _DEFAULT_FETCH_CHARS)

    total = len(content)
    window = content[offset : offset + limit]
    end = offset + len(window)
    footer = ""
    if end < total:
        footer = f"\n\n[showing chars {offset}-{end} of {total} total; call again with offset={end} for more]"
    return ToolResult(output=window + footer)


FETCH_ARTIFACT = ToolSpec(
    name="fetch_artifact",
    description="Retrieve the full content of a large tool result that was truncated out of "
    "the conversation and archived (you'll see a note like \"archived as "
    "artifact_id='art_...'\" when this happens). Supports offset/limit to page through very "
    "large artifacts without pulling the whole thing into context at once.",
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "offset": {
                "type": "integer",
                "description": "Character offset to start from (default 0).",
            },
            "limit": {
                "type": "integer",
                "description": "Max characters to return (default 4000).",
            },
        },
        "required": ["artifact_id"],
    },
    handler=_fetch_artifact,
    needs_permission=False,
)
