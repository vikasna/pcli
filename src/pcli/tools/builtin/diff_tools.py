"""diff_files/apply_patch: pure-Python replacements for the diff/patch CLIs
— neither ships with Windows, and shelling out to them would reintroduce
the exact cross-platform fragility download_file was built to avoid for
curl/wget. Built on difflib (diffing) and a small unified-diff applier
(patching, since difflib doesn't include one)."""

from __future__ import annotations

import difflib
import re

from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.builtin.fs_tools import resolve_path

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


async def _diff_files(arguments: dict, ctx: ToolContext) -> ToolResult:
    raw_a, raw_b = arguments["path_a"], arguments["path_b"]
    path_a = resolve_path(raw_a, ctx)
    path_b = resolve_path(raw_b, ctx)

    # Only path_a goes through the automatic guardrail_path_arg check
    # (AgentLoop only threads one path argument per tool call) — path_b
    # needs the same check done by hand here.
    guardrail_result = ctx.guardrails.evaluate_path(str(path_b))
    if not guardrail_result.allowed:
        return ToolResult(output=f"path_b denied: {guardrail_result.reason}", is_error=True)

    for label, raw, resolved in (("path_a", raw_a, path_a), ("path_b", raw_b, path_b)):
        if not resolved.is_file():
            return ToolResult(
                output=f"{label} '{raw}' does not exist or is not a file.\n"
                "[pcli] Suggestion: check the path with list_dir or glob_search before diffing.",
                is_error=True,
            )

    lines_a = path_a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    lines_b = path_b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    context_lines = int(arguments.get("context_lines", 3))
    diff_lines = list(
        difflib.unified_diff(lines_a, lines_b, fromfile=raw_a, tofile=raw_b, n=context_lines)
    )
    if not diff_lines:
        return ToolResult(output="No differences.")
    # A line derived from a file whose last line has no trailing newline
    # comes back from difflib without one too — joining it directly onto
    # the next line would silently merge the two into one bogus line, so
    # every line needs its own terminator regardless of the source file's
    # own trailing-newline status (apply_patch tracks that separately, from
    # the target file it's patching, not from the diff text).
    text = "".join(line if line.endswith("\n") else line + "\n" for line in diff_lines)
    return ToolResult(output=text)


DIFF_FILES = ToolSpec(
    name="diff_files",
    description="Compare two text files and return a unified diff (like `diff -u`), without "
    "depending on a diff CLI being installed. Useful for reviewing a change or comparing two "
    "versions of a file.",
    parameters={
        "type": "object",
        "properties": {
            "path_a": {"type": "string", "description": "First file (the 'before' side)."},
            "path_b": {"type": "string", "description": "Second file (the 'after' side)."},
            "context_lines": {
                "type": "integer",
                "description": "Lines of unchanged context around each change (default 3).",
            },
        },
        "required": ["path_a", "path_b"],
    },
    handler=_diff_files,
    needs_permission=False,
    guardrail_path_arg="path_a",
    plan_mode_safe=True,
)


class _PatchError(Exception):
    pass


def _parse_hunks(patch_text: str) -> list[tuple[int, list[tuple[str, str]]]]:
    """Parses unified-diff hunks into (old_start_line, [(tag, content), ...])
    pairs, tag being ' ' (context), '-' (removed) or '+' (added). File
    header lines (---/+++) and anything before the first '@@' are ignored,
    matching how `diff -u`/`git diff` output looks in practice."""
    lines = patch_text.splitlines()
    hunks: list[tuple[int, list[tuple[str, str]]]] = []
    i = 0
    while i < len(lines):
        match = _HUNK_HEADER_RE.match(lines[i])
        if not match:
            i += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        i += 1
        body: list[tuple[str, str]] = []
        consumed_old = consumed_new = 0
        while i < len(lines) and (consumed_old < old_count or consumed_new < new_count):
            raw = lines[i]
            if raw.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            tag = raw[0] if raw else " "
            content = raw[1:] if raw else ""
            if tag not in (" ", "-", "+"):
                raise _PatchError(f"Unrecognized line in hunk body: {raw!r}")
            body.append((tag, content))
            if tag != "+":
                consumed_old += 1
            if tag != "-":
                consumed_new += 1
            i += 1
        hunks.append((old_start, body))
    if not hunks:
        raise _PatchError(
            "No hunks found — expected unified diff format with '@@ -start,count "
            "+start,count @@' headers."
        )
    return hunks


def _apply_hunks(original_lines: list[str], hunks: list[tuple[int, list[tuple[str, str]]]]) -> list[str]:
    result: list[str] = []
    cursor = 0
    for old_start, body in hunks:
        start_index = old_start - 1
        if start_index < cursor:
            raise _PatchError(
                f"Hunk at line {old_start} overlaps the previous hunk — the patch's hunks must "
                "be in order and non-overlapping."
            )
        if start_index > len(original_lines):
            raise _PatchError(
                f"Hunk at line {old_start} starts past the end of the file "
                f"({len(original_lines)} lines)."
            )
        result.extend(original_lines[cursor:start_index])
        cursor = start_index
        for tag, content in body:
            actual = original_lines[cursor] if cursor < len(original_lines) else None
            if tag == " ":
                if actual != content:
                    raise _PatchError(
                        f"Context mismatch at line {cursor + 1}: patch expects {content!r}, "
                        f"file has {actual!r}."
                    )
                result.append(content)
                cursor += 1
            elif tag == "-":
                if actual != content:
                    raise _PatchError(
                        f"Line to remove doesn't match at line {cursor + 1}: patch expects "
                        f"{content!r}, file has {actual!r}."
                    )
                cursor += 1
            else:  # "+"
                result.append(content)
    result.extend(original_lines[cursor:])
    return result


async def _apply_patch(arguments: dict, ctx: ToolContext) -> ToolResult:
    resolved = resolve_path(arguments["path"], ctx)
    if not resolved.is_file():
        return ToolResult(
            output=f"{resolved} doesn't exist — use write_file to create it instead of patching "
            "a file that isn't there yet.",
            is_error=True,
        )

    original_text = resolved.read_text(encoding="utf-8")
    original_lines = original_text.splitlines()
    had_trailing_newline = original_text == "" or original_text.endswith("\n")

    try:
        hunks = _parse_hunks(arguments["patch"])
        new_lines = _apply_hunks(original_lines, hunks)
    except _PatchError as exc:
        return ToolResult(
            output=f"Failed to apply patch: {exc}\n"
            "[pcli] Suggestion: this tool requires an exact match against the file's current "
            "content (no fuzzy offsets like the `patch` CLI) — the file has likely changed since "
            "the patch was generated. Read the file with read_file, regenerate the diff against "
            "its current content (diff_files), or use edit_file for a single targeted change "
            "instead.",
            is_error=True,
        )

    new_text = "\n".join(new_lines)
    if new_lines and had_trailing_newline:
        new_text += "\n"
    resolved.write_text(new_text, encoding="utf-8")
    return ToolResult(output=f"Applied patch to {resolved} ({len(hunks)} hunk(s)).")


APPLY_PATCH = ToolSpec(
    name="apply_patch",
    description="Apply a unified diff (as produced by diff_files or `git diff`) to a file, "
    "modifying it in place. Requires the file's current content to exactly match the patch's "
    "context/removed lines — unlike the `patch` CLI, it does not fuzzy-match on offset. Use this "
    "to apply a multi-hunk change in one call instead of several edit_file calls; for a single "
    "small change, edit_file is simpler and more robust.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to patch, relative or absolute."},
            "patch": {
                "type": "string",
                "description": "Unified diff text (containing '@@ ... @@' hunk headers) to apply.",
            },
        },
        "required": ["path", "patch"],
    },
    handler=_apply_patch,
    needs_permission=True,
    risk_description="Modifies a file on disk by applying a patch.",
    guardrail_path_arg="path",
)
