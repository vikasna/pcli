"""In-process filesystem tools (no sandbox needed — plain Python I/O), still
guardrail-gated on the `path` argument."""

from __future__ import annotations

from pathlib import Path

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_MAX_READ_BYTES = 512_000


def _resolve(raw_path: str, ctx: ToolContext) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = ctx.cwd / path
    return path.resolve()


async def _read_file(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = _resolve(arguments["path"], ctx)
    if not resolved.exists():
        return ToolResult(
            output=f"File not found: {resolved}\n"
            "[pcli] Suggestion: if you're not sure of the exact path, use list_dir on its "
            "parent directory or glob_search to locate it.",
            is_error=True,
        )
    if not resolved.is_file():
        return ToolResult(output=f"Not a file: {resolved}", is_error=True)
    data = resolved.read_bytes()
    text = data[:_MAX_READ_BYTES].decode(errors="replace")
    if len(data) > _MAX_READ_BYTES:
        text += "\n[...truncated...]"
    return ToolResult(output=text)


READ_FILE = ToolSpec(
    name="read_file",
    description="Read the contents of a text file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file, relative to the working directory or absolute.",
            }
        },
        "required": ["path"],
    },
    handler=_read_file,
    needs_permission=False,
    guardrail_path_arg="path",
    plan_mode_safe=True,
)


async def _write_file(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = _resolve(arguments["path"], ctx)
    content = arguments.get("content", "")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return ToolResult(output=f"Wrote {len(content)} bytes to {resolved}")


WRITE_FILE = ToolSpec(
    name="write_file",
    description="Write (overwrite) a text file with the given content, creating parent "
    "directories as needed.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
    needs_permission=True,
    risk_description="Writes/overwrites a file on disk.",
    guardrail_path_arg="path",
)


async def _edit_file(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = _resolve(arguments["path"], ctx)
    if not resolved.is_file():
        return ToolResult(
            output=f"{resolved} doesn't exist — use write_file to create it.", is_error=True
        )
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    content = resolved.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        return ToolResult(
            output="old_string not found in file.\n"
            "[pcli] Suggestion: the file may have changed since you last saw it (e.g. an "
            "earlier edit_file/write_file call already changed this part) — read_file to see "
            "its current content before retrying.",
            is_error=True,
        )
    if count > 1:
        return ToolResult(
            output=f"old_string is not unique ({count} occurrences) — include more "
            "surrounding context to make it match exactly once.",
            is_error=True,
        )
    resolved.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
    return ToolResult(output=f"Edited {resolved} (1 replacement).")


EDIT_FILE = ToolSpec(
    name="edit_file",
    description="Replace an exact, unique block of text in an existing file with new text. "
    "old_string must match the file's current content exactly (including whitespace) and "
    "occur exactly once — include enough surrounding context to make it unambiguous. Prefer "
    "this over write_file for small changes to existing files; use write_file for new files "
    "or a genuine full rewrite.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Text to replace it with."},
        },
        "required": ["path", "old_string", "new_string"],
    },
    handler=_edit_file,
    needs_permission=True,
    risk_description="Edits a file on disk.",
    guardrail_path_arg="path",
)


async def _list_dir(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = _resolve(arguments.get("path", "."), ctx)
    if not resolved.is_dir():
        return ToolResult(
            output=f"Not a directory: {resolved}\n"
            "[pcli] Suggestion: list_dir its parent to confirm the correct name/path.",
            is_error=True,
        )
    entries = sorted(resolved.iterdir(), key=lambda p: p.name)
    lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
    return ToolResult(output="\n".join(lines) or "(empty directory)")


LIST_DIR = ToolSpec(
    name="list_dir",
    description="List the contents of a directory.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path (default: working directory)."}
        },
    },
    handler=_list_dir,
    needs_permission=False,
    guardrail_path_arg="path",
    plan_mode_safe=True,
)


async def _glob_search(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved_base = _resolve(arguments.get("path", "."), ctx)
    pattern = arguments["pattern"]
    if not resolved_base.is_dir():
        return ToolResult(
            output=f"Not a directory: {resolved_base}\n"
            "[pcli] Suggestion: list_dir its parent to confirm the correct name/path.",
            is_error=True,
        )
    matches = sorted(
        str(p.relative_to(resolved_base)) for p in resolved_base.glob(pattern) if p.is_file()
    )
    if not matches:
        return ToolResult(output="No matches.")
    truncated = matches[:500]
    text = "\n".join(truncated)
    if len(matches) > 500:
        text += f"\n[...{len(matches) - 500} more matches not shown...]"
    return ToolResult(output=text)


GLOB_SEARCH = ToolSpec(
    name="glob_search",
    description="Find files under a directory matching a glob pattern, e.g. '**/*.py'.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Base directory (default: working directory)."},
        },
        "required": ["pattern"],
    },
    handler=_glob_search,
    needs_permission=False,
    guardrail_path_arg="path",
    plan_mode_safe=True,
)
