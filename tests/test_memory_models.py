"""Coverage for memory/models.py's render_memory_list - the /memory
command's human-facing listing.

Regression coverage for a real reported complaint: /memory rendered as an
unformatted wall of text. Root cause: the TUI always renders a "system"
message through rich.markdown.Markdown (see message_view.py's
_render_message), which collapses plain "\\n"-joined lines with no blank-
line/list-marker structure into a single run-on paragraph. render_memory_list
now emits real Markdown (bold category headers, "- " bulleted entries, a
blank line between category blocks) so it actually renders as a list."""

from rich.console import Console
from rich.markdown import Markdown

from pcli.memory.models import MemoryCategory, MemoryEntry, MemorySource, render_memory_list


def _entry(category: MemoryCategory, content: str, *, source: MemorySource = "derived") -> MemoryEntry:
    return MemoryEntry(category=category, content=content, source=source)


def _rendered_lines(text: str) -> list[str]:
    """What the TUI would actually show - render through the same
    rich.markdown.Markdown path message_view.py uses, then split into
    non-blank lines, so a regression back to one squashed paragraph is
    caught even if the raw markup string still "looks" fine."""
    console = Console(width=100, record=True)
    console.print(Markdown(text))
    return [line for line in console.export_text().splitlines() if line.strip()]


def test_render_memory_list_empty():
    assert render_memory_list([]) == "No memory entries yet."


def test_render_memory_list_groups_by_category_with_markdown_bullets():
    entries = [
        _entry("profile", "Works as a backend Python developer"),
        _entry("preference", "Prefers terse responses"),
        _entry("preference", "Wants an explicit approval for risky commands", source="explicit"),
    ]

    text = render_memory_list(entries)

    assert "**Nature of work:**" in text
    assert "**Preferences:**" in text
    assert "- `" in text  # each entry is a real Markdown bullet, id in a code span
    assert "Works as a backend Python developer" in text
    assert "*(explicit)*" in text  # only the explicit entry is marked
    assert text.count("*(explicit)*") == 1


def test_render_memory_list_entries_render_as_separate_lines_not_one_paragraph():
    """The actual regression: each entry must land on its own rendered
    line, not get squashed together with its category header or neighbors."""
    entries = [
        _entry("profile", "Works as a backend Python developer"),
        _entry("preference", "Prefers terse responses"),
        _entry("preference", "Likes seeing test output before claiming done"),
    ]

    lines = _rendered_lines(render_memory_list(entries))

    profile_line = next(line for line in lines if "backend Python developer" in line)
    pref_lines = [line for line in lines if "Prefers terse" in line or "test output" in line]
    assert len(pref_lines) == 2
    # Different entries, different rendered lines - not merged into one.
    assert pref_lines[0] != pref_lines[1]
    assert profile_line not in pref_lines


def test_render_memory_list_short_id_present_for_forget_command():
    entry = _entry("common_ask", "Often asks for a summary of recent commits")
    text = render_memory_list([entry])
    assert entry.id[-4:] in text
