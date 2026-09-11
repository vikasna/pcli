"""Coverage for Ctrl+G (action_show_subagent_activity): a live-updating
SubagentActivityModal, distinct from /subagent's one-off chat-transcript
snapshot (see test_chat_screen_turn_config.py)."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.screens.subagent_activity_modal import SubagentActivityModal
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path) -> tuple[ChatScreen, object]:
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
async def test_ctrl_g_with_no_subagent_running_reports_that_in_chat(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+g")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "No subagent is currently running" in message_view._current_text
        assert not isinstance(app.screen, SubagentActivityModal)


@pytest.mark.asyncio
async def test_ctrl_g_with_a_running_subagent_opens_the_modal_with_its_detail(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        screen._activity.record_subagent_tool_call("read_file", '{"path": "data.csv"}')

        await pilot.press("ctrl+g")
        await pilot.pause()

        assert isinstance(app.screen, SubagentActivityModal)
        body_text = app.screen.query_one("#subagent-activity-body").render()
        assert "build the report" in str(body_text)
        assert "read_file" in str(body_text)


@pytest.mark.asyncio
async def test_modal_updates_live_while_open(tmp_path: Path):
    """The whole point of Ctrl+G over /subagent: it keeps refreshing while
    left open, instead of being a one-off snapshot."""
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        await pilot.press("ctrl+g")
        await pilot.pause()

        modal = app.screen
        assert isinstance(modal, SubagentActivityModal)
        before = str(modal.query_one("#subagent-activity-body").render())
        assert "run_shell" not in before

        screen._activity.record_subagent_tool_call("run_shell", '{"command": "python train.py"}')
        await pilot.pause()

        after = str(modal.query_one("#subagent-activity-body").render())
        assert "run_shell" in after


@pytest.mark.asyncio
async def test_escape_dismisses_the_modal(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert isinstance(app.screen, SubagentActivityModal)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, SubagentActivityModal)


@pytest.mark.asyncio
async def test_clicking_close_button_dismisses_the_modal(tmp_path: Path):
    """Regression coverage: the Close button previously rendered as plain
    text inside the body ("[Esc] Close") rather than a real Button, so
    clicking it did nothing."""
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert isinstance(app.screen, SubagentActivityModal)

        await pilot.click("#subagent-activity-close-button")
        await pilot.pause()

        assert not isinstance(app.screen, SubagentActivityModal)


@pytest.mark.asyncio
async def test_modal_hints_escape_returns_to_main_window(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        await pilot.press("ctrl+g")
        await pilot.pause()

        modal = app.screen
        assert isinstance(modal, SubagentActivityModal)
        title_text = str(modal.query_one("#subagent-activity-title").render())
        assert "Esc" in title_text
        assert "main agent's window" in title_text


@pytest.mark.asyncio
async def test_status_bar_still_updates_after_the_modal_closes(tmp_path: Path):
    """Regression coverage for the exact bug this feature's design had to
    avoid: ActivityTracker used to support only one subscriber, so opening
    the modal (a second subscriber) would silently steal the slot the
    status bar's own subscription (_on_activity_changed) already held."""
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        await pilot.press("ctrl+g")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        screen._activity.start_subagent("second task")
        await pilot.pause()

        from pcli.tui.widgets.status_bar import StatusBar

        status_bar = screen.query_one(StatusBar)
        assert status_bar.subagent_task == "second task"


@pytest.mark.asyncio
async def test_modal_shows_finished_note_if_subagent_finishes_while_open(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._activity.start_subagent("build the report")
        await pilot.press("ctrl+g")
        await pilot.pause()

        screen._activity.finish_subagent()
        await pilot.pause()

        modal = app.screen
        assert isinstance(modal, SubagentActivityModal)
        text = str(modal.query_one("#subagent-activity-body").render())
        assert "finished" in text.lower()
