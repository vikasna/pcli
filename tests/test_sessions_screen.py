"""Coverage for SessionListScreen.on_mount pruning empty sessions before
listing them (SessionStore.prune_empty_sessions), excluding whichever
session opened this screen (exclude_session_id) since it may still
legitimately have zero messages at this point."""

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import ListView

from pcli.session.models import Message
from pcli.session.store import SessionStore
from pcli.tui.screens.sessions import SessionListScreen


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
