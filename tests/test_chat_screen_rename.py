"""Coverage for `/rename`: sets Session.title, which derive_title() (used by
the /sessions list) prefers over the auto-derived first-message snippet."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _notification_messages(app) -> list[str]:
    """Toast messages currently shown (App._notifications) - see chat.py's
    self.notify(...) calls, Textual's built-in transient-notice mechanism
    used for ephemeral command confirmations instead of the transcript."""
    return [n.message for n in app._notifications]


def _make_screen(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


@pytest.mark.asyncio
async def test_rename_with_no_argument_reports_current_title(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/rename")
        await pilot.pause()

        assert any("New session" in m for m in _notification_messages(app))  # the derived default


@pytest.mark.asyncio
async def test_rename_sets_title_and_persists(tmp_path: Path):
    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/rename Investigating flaky CI")
        await pilot.pause()

        assert any("Investigating flaky CI" in m for m in _notification_messages(app))
        assert session.title == "Investigating flaky CI"

        # Persisted to the session index, which /sessions reads from.
        index = screen._store.list_index()
        entry = next(e for e in index if e.id == session.id)
        assert entry.title == "Investigating flaky CI"


@pytest.mark.asyncio
async def test_rename_then_no_argument_shows_the_new_title(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/rename my custom title")
        await pilot.pause()
        app.clear_notifications()
        screen._handle_command("/rename")
        await pilot.pause()

        assert any("my custom title" in m for m in _notification_messages(app))
