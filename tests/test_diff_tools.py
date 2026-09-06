"""Coverage for diff_files/apply_patch: pure-Python replacements for the
diff/patch CLIs, which don't ship with Windows."""

from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.diff_tools import APPLY_PATCH, DIFF_FILES


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("diff_files/apply_patch should never touch the sandbox")


def _ctx(cwd: Path, *, allowed_roots: list[str] | None = None) -> ToolContext:
    guardrails = GuardrailsConfig(fs_allowed_roots=allowed_roots or [str(cwd)])
    return ToolContext(sandbox=_NullSandbox(), guardrails=guardrails, cwd=cwd)


@pytest.mark.asyncio
async def test_diff_files_reports_a_unified_diff(tmp_path: Path):
    (tmp_path / "a.txt").write_text("line1\nline2\nline3\n")
    (tmp_path / "b.txt").write_text("line1\nCHANGED\nline3\nline4\n")

    result = await DIFF_FILES.handler({"path_a": "a.txt", "path_b": "b.txt"}, _ctx(tmp_path))

    assert result.is_error is False
    assert "-line2" in result.output
    assert "+CHANGED" in result.output
    assert "+line4" in result.output


@pytest.mark.asyncio
async def test_diff_files_reports_no_differences_for_identical_files(tmp_path: Path):
    (tmp_path / "a.txt").write_text("same\n")
    (tmp_path / "b.txt").write_text("same\n")

    result = await DIFF_FILES.handler({"path_a": "a.txt", "path_b": "b.txt"}, _ctx(tmp_path))

    assert result.is_error is False
    assert result.output == "No differences."


@pytest.mark.asyncio
async def test_diff_files_errors_when_a_file_is_missing(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi\n")

    result = await DIFF_FILES.handler(
        {"path_a": "a.txt", "path_b": "missing.txt"}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "does not exist" in result.output
    assert "[pcli] Suggestion" in result.output


@pytest.mark.asyncio
async def test_diff_files_enforces_guardrails_on_path_b_too(tmp_path: Path):
    outside = tmp_path.parent / "outside_diff_test.txt"
    outside.write_text("secret\n")
    (tmp_path / "a.txt").write_text("hi\n")

    try:
        result = await DIFF_FILES.handler(
            {"path_a": "a.txt", "path_b": str(outside)}, _ctx(tmp_path)
        )
        assert result.is_error is True
        assert "denied" in result.output
    finally:
        outside.unlink()


@pytest.mark.asyncio
async def test_apply_patch_round_trips_a_diff_files_result(tmp_path: Path):
    (tmp_path / "a.txt").write_text("line1\nline2\nline3\n")
    (tmp_path / "b.txt").write_text("line1\nCHANGED\nline3\nline4\n")
    ctx = _ctx(tmp_path)

    diff_result = await DIFF_FILES.handler({"path_a": "a.txt", "path_b": "b.txt"}, ctx)
    patch_result = await APPLY_PATCH.handler(
        {"path": "a.txt", "patch": diff_result.output}, ctx
    )

    assert patch_result.is_error is False
    assert (tmp_path / "a.txt").read_text() == (tmp_path / "b.txt").read_text()


@pytest.mark.asyncio
async def test_apply_patch_handles_multiple_hunks(tmp_path: Path):
    original = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    (tmp_path / "a.txt").write_text(original)
    modified_lines = [f"line{i}" for i in range(1, 21)]
    modified_lines[1] = "CHANGED_NEAR_TOP"
    modified_lines[17] = "CHANGED_NEAR_BOTTOM"
    (tmp_path / "b.txt").write_text("\n".join(modified_lines) + "\n")
    ctx = _ctx(tmp_path)

    diff_result = await DIFF_FILES.handler(
        {"path_a": "a.txt", "path_b": "b.txt", "context_lines": 1}, ctx
    )
    assert diff_result.output.count("@@") == 4  # two separate hunks

    patch_result = await APPLY_PATCH.handler(
        {"path": "a.txt", "patch": diff_result.output}, ctx
    )

    assert patch_result.is_error is False
    assert "2 hunk(s)" in patch_result.output
    assert (tmp_path / "a.txt").read_text() == (tmp_path / "b.txt").read_text()


@pytest.mark.asyncio
async def test_apply_patch_reports_a_clear_error_on_context_mismatch(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo\nbar\nbaz\n")
    bad_patch = (
        "--- a.txt\n+++ a.txt\n@@ -1,3 +1,3 @@\n foo\n-WRONG\n+bar2\n baz\n"
    )

    result = await APPLY_PATCH.handler({"path": "a.txt", "patch": bad_patch}, _ctx(tmp_path))

    assert result.is_error is True
    assert "Context mismatch" in result.output or "doesn't match" in result.output
    assert "'WRONG'" in result.output
    assert "'bar'" in result.output
    assert "[pcli] Suggestion" in result.output
    # The file must be untouched on a failed patch.
    assert (tmp_path / "a.txt").read_text() == "foo\nbar\nbaz\n"


@pytest.mark.asyncio
async def test_apply_patch_errors_on_missing_file(tmp_path: Path):
    result = await APPLY_PATCH.handler(
        {"path": "missing.txt", "patch": "@@ -1,1 +1,1 @@\n-a\n+b\n"}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "doesn't exist" in result.output


@pytest.mark.asyncio
async def test_apply_patch_errors_on_patch_with_no_hunks(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo\n")

    result = await APPLY_PATCH.handler(
        {"path": "a.txt", "patch": "not a real patch"}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "No hunks found" in result.output


@pytest.mark.asyncio
async def test_apply_patch_preserves_no_trailing_newline(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo\nbar")  # no trailing newline
    (tmp_path / "b.txt").write_text("foo\nbaz")  # no trailing newline
    ctx = _ctx(tmp_path)

    diff_result = await DIFF_FILES.handler({"path_a": "a.txt", "path_b": "b.txt"}, ctx)
    patch_result = await APPLY_PATCH.handler(
        {"path": "a.txt", "patch": diff_result.output}, ctx
    )

    assert patch_result.is_error is False
    assert (tmp_path / "a.txt").read_text() == "foo\nbaz"


def test_diff_and_patch_tools_are_registered_in_the_default_registry():
    from pcli.tools.registry import build_default_registry

    registry = build_default_registry()
    assert registry.get("diff_files") is not None
    assert registry.get("apply_patch") is not None
