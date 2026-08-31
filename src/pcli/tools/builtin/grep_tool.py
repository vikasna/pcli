"""In-process regex search across text files under a directory."""

from __future__ import annotations

import re
from pathlib import Path

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_MAX_MATCHES = 200
_MAX_FILES_SCANNED = 5000


def _resolve(raw_path: str, ctx: ToolContext) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = ctx.cwd / path
    return path.resolve()


async def _grep(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved_base = _resolve(arguments.get("path", "."), ctx)
    glob_pattern = arguments.get("glob", "**/*")

    try:
        regex = re.compile(arguments["pattern"])
    except re.error as exc:
        return ToolResult(
            output=f"Invalid regex: {exc}\n"
            "[pcli] Suggestion: if you don't need regex features, escape the special "
            "character(s) or search for a plain substring instead.",
            is_error=True,
        )

    if not resolved_base.is_dir():
        return ToolResult(
            output=f"Not a directory: {resolved_base}\n"
            "[pcli] Suggestion: list_dir its parent to confirm the correct name/path.",
            is_error=True,
        )

    matches: list[str] = []
    scanned = 0
    for file_path in resolved_base.glob(glob_pattern):
        if not file_path.is_file():
            continue
        scanned += 1
        if scanned > _MAX_FILES_SCANNED:
            break
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = file_path.relative_to(resolved_base)
                matches.append(f"{rel}:{line_number}: {line.strip()[:200]}")
                if len(matches) >= _MAX_MATCHES:
                    break
        if len(matches) >= _MAX_MATCHES:
            break

    if not matches:
        return ToolResult(output="No matches.")
    text = "\n".join(matches)
    if len(matches) >= _MAX_MATCHES:
        text += "\n[...match limit reached, results may be incomplete...]"
    return ToolResult(output=text)


GREP = ToolSpec(
    name="grep",
    description="Search for a regex pattern across text files under a directory.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": "Base directory (default: working directory)."},
            "glob": {
                "type": "string",
                "description": "Glob to select which files to scan (default: '**/*').",
            },
        },
        "required": ["pattern"],
    },
    handler=_grep,
    needs_permission=False,
    guardrail_path_arg="path",
    plan_mode_safe=True,
)
