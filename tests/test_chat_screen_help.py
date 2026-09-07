"""Coverage for /help: a static reference of every slash command and shell
prefix."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


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
async def test_help_lists_every_slash_command(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/help")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        text = message_view._current_text
        for command in (
            "/help",
            "/sessions",
            "/export",
            "/models",
            "/compact",
            "/timeout",
            "/context-limit",
            "/max-tool-iterations",
            "/artifact-threshold",
            "/max-tool-calls-per-turn",
            "/max-tool-calls-per-minute",
            "/prune-tool-results",
            "/max-response-tokens",
            "/rename",
            "/plan",
            "/build",
            "/toolbox",
        ):
            assert command in text


@pytest.mark.asyncio
async def test_help_mentions_shell_passthrough_and_input_box_behavior(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/help")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        text = message_view._current_text
        assert "!command" in text
        assert "!!command" in text
        assert "!!!command" in text
        assert "Ctrl+J" in text
