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
        return ToolResult(output=f"File not found: {resolved}", is_error=True)
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


async def _list_dir(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = _resolve(arguments.get("path", "."), ctx)
    if not resolved.is_dir():
        return ToolResult(output=f"Not a directory: {resolved}", is_error=True)
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
)


async def _glob_search(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved_base = _resolve(arguments.get("path", "."), ctx)
    pattern = arguments["pattern"]
    if not resolved_base.is_dir():
        return ToolResult(output=f"Not a directory: {resolved_base}", is_error=True)
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
)
