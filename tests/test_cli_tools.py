"""Coverage for `pcli tools list` - a static, human-facing listing of the
built-in tool registry (name, read-only/mutating type, needs_permission,
plan_mode_safe, and a one-line description). Pure introspection: no
gateway/config/session needed, unlike most other CLI commands."""

from typer.testing import CliRunner

from pcli.cli import _one_line_description, app
from pcli.tools.registry import build_default_registry

runner = CliRunner()


def test_tools_list_shows_every_builtin_tool():
    result = runner.invoke(app, ["tools", "list"])
    assert result.exit_code == 0

    expected_names = {tool.name for tool in build_default_registry()}
    for name in expected_names:
        assert name in result.output


def test_tools_list_marks_read_only_and_mutating_tools_correctly():
    result = runner.invoke(app, ["tools", "list"])
    assert result.exit_code == 0

    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "[read-only" in lines["read_file"]
    assert "[mutating" in lines["write_file"]


def test_tools_list_shows_needs_permission_and_plan_mode_safe():
    result = runner.invoke(app, ["tools", "list"])
    assert result.exit_code == 0

    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "needs_permission=False" in lines["read_file"]
    assert "plan_mode_safe=True" in lines["read_file"]
    assert "needs_permission=True" in lines["write_file"]
    assert "plan_mode_safe=False" in lines["write_file"]


def test_tools_list_output_is_alphabetical():
    result = runner.invoke(app, ["tools", "list"])
    assert result.exit_code == 0

    names = [line.split()[0] for line in result.output.splitlines() if line.strip()]
    assert names == sorted(names)


# --- _one_line_description ---


def test_one_line_description_passes_a_short_description_through_unchanged():
    assert _one_line_description("List the contents of a directory.") == (
        "List the contents of a directory."
    )


def test_one_line_description_drops_everything_after_the_first_paragraph():
    description = "First paragraph, kept.\n\nSecond paragraph, dropped entirely."
    assert _one_line_description(description) == "First paragraph, kept."


def test_one_line_description_truncates_a_too_long_single_paragraph_at_a_word_boundary():
    description = "word " * 40  # no internal newline at all, well past max_chars
    result = _one_line_description(description, max_chars=20)
    assert result.endswith("...")
    assert len(result) <= 23  # 20 + "..."
    assert not result[:-3].endswith(" ")  # truncated at a word boundary, not mid-word
