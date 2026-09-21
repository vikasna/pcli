"""In-process filesystem tools (no sandbox needed — plain Python I/O), still
guardrail-gated on the `path` argument."""

from __future__ import annotations

from pathlib import Path

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_MAX_READ_BYTES = 512_000


def resolve_path(raw_path: str, ctx: ToolContext) -> Path:
    """Resolves a tool-supplied path against the working directory —
    shared with network_tools.py, since download_file needs the exact
    same relative-vs-absolute handling as every other path-taking tool."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = ctx.cwd / path
    return path.resolve()


def not_a_directory_result(resolved: Path) -> ToolResult:
    """Shared "this path isn't usable as a directory" error for list_dir/
    glob_search/grep (grep_tool.py imports this too) - distinguishes two
    genuinely different situations that a flat "Not a directory" message
    used to collapse into one: the path doesn't exist at all (wrong name/
    location - point at how to locate it) vs. it exists but is a file (the
    caller likely meant to target that file directly, not search/list a
    directory - point at read_file instead)."""
    if not resolved.exists():
        return ToolResult(
            output=f"{resolved} does not exist.\n"
            "[pcli] Suggestion: check the path — list_dir its parent directory to see "
            "what's actually there, or glob_search for the name if you're not sure "
            "exactly where it is.",
            is_error=True,
        )
    return ToolResult(
        output=f"{resolved} is a file, not a directory.\n"
        "[pcli] Suggestion: use read_file to read it directly instead.",
        is_error=True,
    )


async def _read_file(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = resolve_path(arguments["path"], ctx)
    if not resolved.exists():
        return ToolResult(
            output=f"File not found: {resolved}\n"
            "[pcli] Suggestion: if you're not sure of the exact path, use list_dir on its "
            "parent directory or glob_search to locate it.",
            is_error=True,
        )
    if not resolved.is_file():
        return ToolResult(
            output=f"Not a file: {resolved} is a directory.\n"
            "[pcli] Suggestion: use list_dir to see what's inside it instead.",
            is_error=True,
        )
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
    resolved = resolve_path(arguments["path"], ctx)
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


def _split_keepends(content: str) -> list[str]:
    """splitlines(keepends=True) — used by insert/delete mode so a file's
    existing line-ending style (or a missing trailing newline on the last
    line) round-trips exactly, instead of normalizing to "\\n" everywhere."""
    return content.splitlines(keepends=True)


async def _edit_file(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = resolve_path(arguments["path"], ctx)
    if not resolved.is_file():
        return ToolResult(
            output=f"{resolved} doesn't exist — use write_file to create it.", is_error=True
        )

    old_string = arguments.get("old_string")
    new_string = arguments.get("new_string")
    insert_after_line = arguments.get("insert_after_line")
    delete_start_line = arguments.get("delete_start_line")
    delete_end_line = arguments.get("delete_end_line")

    modes_given = sum(
        (
            old_string is not None,
            insert_after_line is not None,
            delete_start_line is not None or delete_end_line is not None,
        )
    )
    if modes_given == 0:
        return ToolResult(
            output="Provide exactly one editing mode: old_string (+ new_string) to replace, "
            "insert_after_line (+ new_string) to insert, or delete_start_line + "
            "delete_end_line to delete lines.",
            is_error=True,
        )
    if modes_given > 1:
        return ToolResult(
            output="Provide exactly one editing mode at a time — old_string, "
            "insert_after_line, and delete_start_line/delete_end_line are mutually "
            "exclusive, not combinable in one call.",
            is_error=True,
        )

    content = resolved.read_text(encoding="utf-8")

    if old_string is not None:
        if new_string is None:
            return ToolResult(output="new_string is required alongside old_string.", is_error=True)
        if old_string == new_string:
            return ToolResult(
                output="old_string and new_string are identical — this call would change "
                "nothing.\n[pcli] Suggestion: to insert new text without removing anything, "
                "use insert_after_line + new_string instead (e.g. insert_after_line=12, "
                "new_string='the new line(s) to add') — don't repeat the same anchor text as "
                "both old_string and new_string expecting it to insert around it.",
                is_error=True,
            )
        replace_all = bool(arguments.get("replace_all", False))
        count = content.count(old_string)
        if count == 0:
            return ToolResult(
                output="old_string not found in file.\n"
                "[pcli] Suggestion: old_string must match the file's current content exactly, "
                "including whitespace — the file may also have changed since you last saw it "
                "(e.g. an earlier edit_file/write_file call already changed this part). "
                "read_file to see its current content before retrying.",
                is_error=True,
            )
        if count > 1 and not replace_all:
            return ToolResult(
                output=f"old_string is not unique ({count} occurrences) — include more "
                "surrounding context to make it match exactly once, or pass "
                "replace_all=true to replace every occurrence.",
                is_error=True,
            )
        new_content = content.replace(old_string, new_string, -1 if replace_all else 1)
        resolved.write_text(new_content, encoding="utf-8")
        label = f"{count} replacement(s)" if replace_all else "1 replacement"
        return ToolResult(output=f"Edited {resolved} ({label}).")

    lines = _split_keepends(content)
    total_lines = len(lines)

    if insert_after_line is not None:
        if not new_string:
            return ToolResult(
                output="new_string (the text to insert) is required alongside "
                "insert_after_line.",
                is_error=True,
            )
        if not isinstance(insert_after_line, int) or insert_after_line < 0 or insert_after_line > total_lines:
            return ToolResult(
                output=f"insert_after_line must be an integer between 0 (start of file) and "
                f"{total_lines} (end of file) — the file currently has {total_lines} line(s).",
                is_error=True,
            )
        # The line being inserted after must end with a newline, or the
        # inserted text would be glued onto its end instead of starting a
        # new line — only relevant for a file whose last line has no
        # trailing newline, and only when inserting after that exact line.
        if insert_after_line > 0 and not lines[insert_after_line - 1].endswith("\n"):
            lines[insert_after_line - 1] += "\n"
        insert_text = new_string if new_string.endswith("\n") else new_string + "\n"
        lines.insert(insert_after_line, insert_text)
        resolved.write_text("".join(lines), encoding="utf-8")
        where = "the start of the file" if insert_after_line == 0 else f"line {insert_after_line}"
        return ToolResult(output=f"Inserted new text after {where} in {resolved}.")

    # delete mode
    if delete_start_line is None or delete_end_line is None:
        return ToolResult(
            output="delete_start_line and delete_end_line are both required together.",
            is_error=True,
        )
    if (
        not isinstance(delete_start_line, int)
        or not isinstance(delete_end_line, int)
        or delete_start_line < 1
        or delete_end_line < delete_start_line
        or delete_end_line > total_lines
    ):
        return ToolResult(
            output=f"Invalid line range {delete_start_line}-{delete_end_line} — the file has "
            f"{total_lines} line(s); delete_start_line must be >= 1 and <= delete_end_line, "
            f"and delete_end_line must be <= {total_lines}.",
            is_error=True,
        )
    removed = delete_end_line - delete_start_line + 1
    del lines[delete_start_line - 1 : delete_end_line]
    resolved.write_text("".join(lines), encoding="utf-8")
    return ToolResult(
        output=f"Deleted line(s) {delete_start_line}-{delete_end_line} ({removed} line(s)) "
        f"from {resolved}."
    )


EDIT_FILE = ToolSpec(
    name="edit_file",
    description="Edit an existing file using exactly one of three modes. (1) Replace — "
    "old_string + new_string: replaces an exact, unique block of text (old_string must match "
    "the file's current content verbatim, including whitespace — read_file first if unsure; "
    "include enough surrounding context to make it unambiguous, or pass replace_all=true to "
    "replace every occurrence instead of requiring uniqueness). (2) Insert — "
    "insert_after_line + new_string: inserts new text after a given 1-indexed line "
    "(0 = start of file) without removing anything — use this to add a new section/line, "
    "never the replace mode with new_string equal to old_string. (3) Delete — "
    "delete_start_line + delete_end_line: removes an inclusive range of 1-indexed lines. "
    "Line numbers shift after any edit — re-check with read_file or grep -n before a second "
    "line-based edit to the same file. Use write_file instead for a new file or a genuine "
    "full rewrite.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {
                "type": "string",
                "description": "Replace mode: exact text to replace, matching the file's "
                "current content verbatim. Do not set this equal to new_string to try to "
                "'insert' text around it — use insert_after_line for that instead.",
            },
            "new_string": {
                "type": "string",
                "description": "Replace mode: the replacement text. Insert mode: the new "
                "text to insert.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace mode only: replace every occurrence of old_string "
                "instead of requiring it to match exactly once (default false).",
            },
            "insert_after_line": {
                "type": "integer",
                "description": "Insert mode: 1-indexed line number to insert new_string "
                "after (0 = insert at the very start of the file, before line 1).",
            },
            "delete_start_line": {
                "type": "integer",
                "description": "Delete mode: first 1-indexed line to delete (inclusive). "
                "Required together with delete_end_line.",
            },
            "delete_end_line": {
                "type": "integer",
                "description": "Delete mode: last 1-indexed line to delete (inclusive). "
                "Required together with delete_start_line.",
            },
        },
        "required": ["path"],
    },
    handler=_edit_file,
    needs_permission=True,
    risk_description="Edits a file on disk.",
    guardrail_path_arg="path",
)


async def _list_dir(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = resolve_path(arguments.get("path", "."), ctx)
    if not resolved.is_dir():
        return not_a_directory_result(resolved)
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
    resolved_base = resolve_path(arguments.get("path", "."), ctx)
    pattern = arguments["pattern"]
    if not resolved_base.is_dir():
        return not_a_directory_result(resolved_base)
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
