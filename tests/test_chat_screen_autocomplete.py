"""Integration coverage for slash-command autocomplete wired into
ChatScreen: ChatInput.SuggestionsChanged reaching the real
CommandSuggestions widget via on_chat_input_suggestions_changed, and the
_SLASH_COMMANDS list itself staying in sync with what /help documents and
what _handle_command actually dispatches (see test_chat_input.py and
test_command_suggestions.py for the two widgets' own unit coverage)."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import _SLASH_COMMANDS, ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.command_suggestions import CommandSuggestions


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path) -> ChatScreen:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )
    return ChatScreen(settings, session=session, store=store)


@pytest.mark.asyncio
async def test_typing_a_slash_command_shows_suggestions_in_the_real_screen(tmp_path: Path):
    screen = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        chat_input = screen.query_one(ChatInput)
        suggestions = screen.query_one(CommandSuggestions)
        assert suggestions.styles.display == "none"

        chat_input.focus()
        await pilot.press("/", "c", "o", "m", "p")
        await pilot.pause()

        assert suggestions.styles.display == "block"
        assert len(suggestions.children) >= 1


@pytest.mark.asyncio
async def test_accepting_a_suggestion_then_submitting_dispatches_the_real_command(
    tmp_path: Path,
):
    screen = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        chat_input = screen.query_one(ChatInput)
        chat_input.focus()
        await pilot.press("/", "h", "e", "l", "p")
        await pilot.pause()
        await pilot.press("tab")  # accept -> "/help "
        await pilot.pause()
        await pilot.press("enter")  # now actually submits
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        assert "Commands" in screen.query_one(MessageView)._current_text


def test_every_dispatched_command_has_an_autocomplete_entry():
    """_handle_command's dispatch table and _SLASH_COMMANDS must stay in
    sync - a command missing here would silently never autocomplete."""
    dispatched = {
        "sessions", "export", "toolbox", "models", "compact", "timeout",
        "temperature", "budget", "context-limit", "max-tool-iterations", "artifact-threshold",
        "max-tool-calls-per-turn", "max-tool-calls-per-minute", "allowed-roots",
        "prune-tool-results", "subagent", "memory", "max-response-tokens", "rename",
        "theme", "plan", "build", "help",
    }
    listed = {name for name, _ in _SLASH_COMMANDS}
    assert listed == dispatched


def test_slash_commands_have_no_duplicates_and_nonempty_descriptions():
    names = [name for name, _ in _SLASH_COMMANDS]
    assert len(names) == len(set(names))
    assert all(desc.strip() for _, desc in _SLASH_COMMANDS)
