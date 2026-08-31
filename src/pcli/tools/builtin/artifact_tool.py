"""fetch_artifact: retrieves the full (or a windowed slice of the) content of
a large tool result that AgentLoop truncated out of the live conversation
and archived — see pcli.tools.artifacts and agent/loop.py's dispatch logic."""

from __future__ import annotations

import re

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_DEFAULT_FETCH_CHARS = 4000
_MAX_PATTERN_MATCHES = 50
_DEFAULT_CONTEXT_LINES = 2


def _grep_with_context(
    content: str, pattern: str, context_lines: int, max_chars: int
) -> tuple[str, int]:
    """Grep-style search within an already-fetched artifact: finds every
    line matching `pattern`, keeps context_lines of surrounding lines per
    match (like grep -C), and merges overlapping/adjacent windows so
    nearby matches don't duplicate shared lines. Returns (rendered text,
    total match count) — the caller reports the count separately since it
    may exceed what's actually rendered (capped at _MAX_PATTERN_MATCHES
    matches and max_chars of output)."""
    lines = content.splitlines()
    regex = re.compile(pattern)
    match_indices = [i for i, line in enumerate(lines) if regex.search(line)]
    total_matches = len(match_indices)

    windows: list[tuple[int, int]] = []
    for i in match_indices[:_MAX_PATTERN_MATCHES]:
        start = max(0, i - context_lines)
        end = min(len(lines) - 1, i + context_lines)
        if windows and start <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))

    blocks = [
        "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(start, end + 1))
        for start, end in windows
    ]
    text = "\n--\n".join(blocks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...output truncated to max_chars...]"
    return text, total_matches


async def _fetch_artifact(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.artifact_store is None:
        return ToolResult(output="No artifact store available in this context.", is_error=True)

    artifact_id = arguments["artifact_id"]
    content = ctx.artifact_store.get(artifact_id)
    if content is None:
        return ToolResult(
            output=f"No artifact found with id '{artifact_id}'.\n"
            "[pcli] Suggestion: re-check the \"archived as artifact_id='art_...'\" note in the "
            "original tool result rather than guessing an id — artifact ids aren't derivable "
            "any other way.",
            is_error=True,
        )

    limit = int(arguments.get("limit") or _DEFAULT_FETCH_CHARS)
    pattern = arguments.get("pattern")

    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            return ToolResult(
                output=f"Invalid regex: {exc}\n"
                "[pcli] Suggestion: if you don't need regex features, escape the special "
                "character(s) or search for a plain substring instead.",
                is_error=True,
            )
        context_lines = int(arguments.get("context_lines") or _DEFAULT_CONTEXT_LINES)
        text, total_matches = _grep_with_context(content, pattern, context_lines, limit)
        if total_matches == 0:
            return ToolResult(output=f"No lines matching {pattern!r} found in this artifact.")
        header = f"{total_matches} matching line(s)"
        if total_matches > _MAX_PATTERN_MATCHES:
            header += f" (showing first {_MAX_PATTERN_MATCHES})"
        return ToolResult(output=f"{header}:\n{text}")

    offset = max(0, int(arguments.get("offset") or 0))
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
    "large artifacts without pulling the whole thing into context at once, or a grep-style "
    "pattern to jump straight to the part you need.",
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "pattern": {
                "type": "string",
                "description": "Regex — if given, returns only matching lines plus "
                "context_lines of surrounding context (like grep -C), instead of a raw "
                "character slice. Prefer this over offset/limit when you know what you're "
                "looking for — offset is ignored when pattern is given.",
            },
            "context_lines": {
                "type": "integer",
                "description": "Lines of context before/after each match when pattern is "
                "given (default 2).",
            },
            "offset": {
                "type": "integer",
                "description": "Character offset to start from (default 0). Ignored when "
                "pattern is given.",
            },
            "limit": {
                "type": "integer",
                "description": "Max characters to return (default 4000). Also caps output "
                "when pattern is given.",
            },
        },
        "required": ["artifact_id"],
    },
    handler=_fetch_artifact,
    needs_permission=False,
    plan_mode_safe=True,
)
