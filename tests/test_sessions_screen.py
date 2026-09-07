"""Coverage for SessionListScreen.on_mount pruning empty sessions before
listing them (SessionStore.prune_empty_sessions), excluding whichever
session opened this screen (exclude_session_id) since it may still
legitimately have zero messages at this point."""

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import ListView, Static

from pcli.session.models import Message
from pcli.session.store import SessionStore
from pcli.tui.screens.sessions import SessionListScreen, _sessions_header_row, _truncate


class _HostApp(App):
    def __init__(self, screen: SessionListScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


@pytest.mark.asyncio
async def test_on_mount_prunes_empty_sessions_before_listing(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    empty = store.new_session(model="fake-model")
    real = store.new_session(model="fake-model")
    real.messages.append(Message(role="system", content="system prompt"))
    real.messages.append(Message(role="user", content="hello"))
    store.save(real)

    screen = SessionListScreen(store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert not store.session_dir(empty.id).exists()
        assert store.session_dir(real.id).exists()
        list_view = screen.query_one(ListView)
        assert len(list_view.children) == 1


@pytest.mark.asyncio
async def test_on_mount_excludes_the_session_that_opened_it(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    active_but_still_empty = store.new_session(model="fake-model")

    screen = SessionListScreen(store, exclude_session_id=active_but_still_empty.id)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Survives pruning (not deleted out from under the active ChatScreen)
        # and is still listed, same as any other real index entry - only
        # deletion is suppressed for the excluded id, not visibility.
        assert store.session_dir(active_but_still_empty.id).exists()
        list_view = screen.query_one(ListView)
        assert len(list_view.children) == 1


@pytest.mark.asyncio
async def test_on_mount_with_no_exclusion_prunes_all_empty_sessions(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    empty_one = store.new_session(model="fake-model")
    empty_two = store.new_session(model="fake-model")

    screen = SessionListScreen(store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert not store.session_dir(empty_one.id).exists()
        assert not store.session_dir(empty_two.id).exists()


@pytest.mark.asyncio
async def test_a_header_row_is_shown_above_the_list(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    screen = SessionListScreen(store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        header = screen.query_one("#sessions-header", Static)
        assert header._Static__content == _sessions_header_row()
        assert "ID" in header._Static__content
        assert "TITLE" in header._Static__content
        assert "UPDATED" in header._Static__content


@pytest.mark.asyncio
async def test_each_row_shows_the_last_four_chars_of_the_session_id(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    real = store.new_session(model="fake-model")
    real.messages.append(Message(role="system", content="system prompt"))
    real.messages.append(Message(role="user", content="hello"))
    store.save(real)

    screen = SessionListScreen(store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        list_view = screen.query_one(ListView)
        row_static = list_view.children[0].query_one(Static)
        assert real.id[-4:] in row_static._Static__content
        # The full id must not appear - only the last-4-chars column.
        assert real.id not in row_static._Static__content


def test_truncate_leaves_short_text_untouched():
    assert _truncate("short", 10) == "short"


def test_truncate_shortens_long_text_with_an_ascii_ellipsis():
    result = _truncate("a very long piece of text indeed", 10)
    assert len(result) == 10
    assert result.endswith("...")
