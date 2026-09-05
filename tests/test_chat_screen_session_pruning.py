"""Coverage for ChatScreen.on_mount pruning empty sessions left over from a
previous run (SessionStore.prune_empty_sessions), while never touching the
session it's currently starting/resuming even though that one may still
legitimately have zero messages at mount time."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.models import Message
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )


@pytest.mark.asyncio
async def test_on_mount_prunes_leftover_empty_sessions_from_a_previous_run(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    leftover_empty = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")

    screen = ChatScreen(_settings(), store=store)  # a fresh new session
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert not store.session_dir(leftover_empty.id).exists()


@pytest.mark.asyncio
async def test_on_mount_never_prunes_the_session_it_is_starting(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")

    screen = ChatScreen(_settings(), store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        # The new session has only the in-memory system prompt appended
        # (never re-saved yet) - still zero messages on disk at this point,
        # and must survive its own on_mount's prune pass.
        assert store.session_dir(screen._session.id).exists()


@pytest.mark.asyncio
async def test_on_mount_does_not_prune_a_resumed_session_with_real_content(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages.append(Message(role="system", content="system prompt"))
    session.messages.append(Message(role="user", content="hello"))
    session.messages.append(Message(role="assistant", content="hi"))
    store.save(session)

    screen = ChatScreen(_settings(), session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert store.session_dir(session.id).exists()
