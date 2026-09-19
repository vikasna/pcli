"""Coverage for CommandSuggestions: the dumb rendering widget ChatInput's
autocomplete drives via update_suggestions() - ChatInput itself (see
test_chat_input.py) owns all matching/navigation state; this only covers
what it displays given a matches list and highlighted index."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from pcli.tui.widgets.command_suggestions import CommandSuggestions


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield CommandSuggestions(id="sugg")


def _row_texts(widget: CommandSuggestions) -> list[str]:
    return [item.query_one(Static)._Static__content for item in widget.children]


@pytest.mark.asyncio
async def test_hidden_by_default():
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sugg = app.query_one(CommandSuggestions)
        assert sugg.styles.display == "none"


@pytest.mark.asyncio
async def test_shows_and_lists_matches_with_descriptions():
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sugg = app.query_one(CommandSuggestions)

        sugg.update_suggestions([("compact", "Summarize the conversation.")], 0)
        await pilot.pause()

        assert sugg.styles.display == "block"
        rows = _row_texts(sugg)
        assert len(rows) == 1
        assert "/compact" in rows[0]
        assert "Summarize the conversation." in rows[0]


@pytest.mark.asyncio
async def test_empty_matches_hides_and_clears():
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sugg = app.query_one(CommandSuggestions)
        sugg.update_suggestions([("compact", "desc")], 0)
        await pilot.pause()

        sugg.update_suggestions([], 0)
        await pilot.pause()

        assert sugg.styles.display == "none"
        assert len(sugg.children) == 0


@pytest.mark.asyncio
async def test_highlighted_index_sets_list_view_index():
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sugg = app.query_one(CommandSuggestions)

        sugg.update_suggestions([("a", "d1"), ("b", "d2"), ("c", "d3")], 2)
        await pilot.pause()

        assert sugg.index == 2


@pytest.mark.asyncio
async def test_out_of_range_index_is_clamped():
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sugg = app.query_one(CommandSuggestions)

        sugg.update_suggestions([("a", "d1"), ("b", "d2")], 99)
        await pilot.pause()

        assert sugg.index == 1


@pytest.mark.asyncio
async def test_replacing_matches_clears_prior_rows():
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sugg = app.query_one(CommandSuggestions)

        sugg.update_suggestions([("a", "d1"), ("b", "d2")], 0)
        await pilot.pause()
        sugg.update_suggestions([("c", "d3")], 0)
        await pilot.pause()

        rows = _row_texts(sugg)
        assert len(rows) == 1
        assert "/c" in rows[0]
