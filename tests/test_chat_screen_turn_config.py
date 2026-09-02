"""Coverage for turn/guardrail-related config commands:
/max-tool-iterations and /artifact-threshold (persisted to config.toml the
same way /timeout persists request_timeout_s, pushed live into the running
AgentLoop) and /max-tool-calls-per-turn / /max-tool-calls-per-minute
(persisted to guardrails.toml's [limits] table, pushed live into the
running PermissionManager's guardrails instance)."""

import tomllib
from pathlib import Path

import pytest
from textual.app import App

from pcli.config.paths import config_file
from pcli.config.settings import Settings
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path, **settings_overrides):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


# --- /max-tool-iterations ---


@pytest.mark.asyncio
async def test_max_tool_iterations_with_no_argument_reports_current_value(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-tool-iterations")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "25" in message_view._current_text  # Settings' default


@pytest.mark.asyncio
async def test_max_tool_iterations_sets_persists_and_takes_effect_live(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._agent_loop is not None

        screen._handle_command("/max-tool-iterations 10")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "10" in message_view._current_text
        assert screen._settings.max_tool_iterations == 10
        assert screen._agent_loop._max_tool_iterations == 10

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["max_tool_iterations"] == 10


@pytest.mark.asyncio
async def test_max_tool_iterations_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/max-tool-iterations not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text

        screen._handle_command("/max-tool-iterations -5")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text


@pytest.mark.asyncio
async def test_max_tool_iterations_notes_it_is_ignored_in_local_api_mode(tmp_path: Path):
    screen, _session = _make_screen(
        tmp_path, local_api_gateways=["http://fake-gateway.test/v1"]
    )
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._agent_loop is not None

        screen._handle_command("/max-tool-iterations 10")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "uncapped" in message_view._current_text.lower()
        # Saved anyway, even though it won't take effect while local-api.
        assert screen._settings.max_tool_iterations == 10
        assert screen._agent_loop._max_tool_iterations is None


# --- /artifact-threshold ---


@pytest.mark.asyncio
async def test_artifact_threshold_with_no_argument_reports_current_value(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/artifact-threshold")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "4,000" in message_view._current_text  # Settings' default


@pytest.mark.asyncio
async def test_artifact_threshold_sets_persists_and_takes_effect_live(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._agent_loop is not None

        screen._handle_command("/artifact-threshold 8000")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "8,000" in message_view._current_text
        assert screen._settings.artifact_threshold_chars == 8000
        assert screen._agent_loop._artifact_threshold_chars == 8000

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["artifact_threshold_chars"] == 8000


@pytest.mark.asyncio
async def test_artifact_threshold_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/artifact-threshold not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text

        screen._handle_command("/artifact-threshold -5")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text


# --- /max-tool-calls-per-turn ---


@pytest.mark.asyncio
async def test_max_tool_calls_per_turn_with_no_argument_reports_current_value(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-tool-calls-per-turn")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "25" in message_view._current_text  # GuardrailsConfig's default


@pytest.mark.asyncio
async def test_max_tool_calls_per_turn_sets_persists_and_takes_effect_live(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-tool-calls-per-turn 5")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "5" in message_view._current_text
        assert screen._permission_manager.guardrails.max_tool_calls_per_turn == 5

        reloaded = GuardrailsConfig.load()
        assert reloaded.max_tool_calls_per_turn == 5


@pytest.mark.asyncio
async def test_max_tool_calls_per_turn_accepts_zero_as_unlimited(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-tool-calls-per-turn 0")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "set to 0" in message_view._current_text
        assert screen._permission_manager.guardrails.max_tool_calls_per_turn == 0


@pytest.mark.asyncio
async def test_max_tool_calls_per_turn_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/max-tool-calls-per-turn not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text

        screen._handle_command("/max-tool-calls-per-turn -5")
        await pilot.pause()
        assert "0 or greater" in message_view._current_text


@pytest.mark.asyncio
async def test_max_tool_calls_per_turn_notes_it_is_ignored_in_local_api_mode(tmp_path: Path):
    screen, _session = _make_screen(
        tmp_path, local_api_gateways=["http://fake-gateway.test/v1"]
    )
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        # ChatScreen.__init__ already force-copies this to 0 for local-api.
        assert screen._permission_manager.guardrails.max_tool_calls_per_turn == 0

        screen._handle_command("/max-tool-calls-per-turn 5")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "local-api mode" in message_view._current_text
        # Not applied live - still forced unlimited for this session.
        assert screen._permission_manager.guardrails.max_tool_calls_per_turn == 0
        # But persisted for whenever local-api mode is off.
        reloaded = GuardrailsConfig.load()
        assert reloaded.max_tool_calls_per_turn == 5


# --- /max-tool-calls-per-minute ---


@pytest.mark.asyncio
async def test_max_tool_calls_per_minute_with_no_argument_reports_current_value(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-tool-calls-per-minute")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "60" in message_view._current_text  # GuardrailsConfig's default


@pytest.mark.asyncio
async def test_max_tool_calls_per_minute_sets_persists_and_takes_effect_live(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-tool-calls-per-minute 15")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "15" in message_view._current_text
        assert screen._permission_manager.guardrails.max_tool_calls_per_minute == 15

        reloaded = GuardrailsConfig.load()
        assert reloaded.max_tool_calls_per_minute == 15


@pytest.mark.asyncio
async def test_max_tool_calls_per_minute_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/max-tool-calls-per-minute not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text

        screen._handle_command("/max-tool-calls-per-minute -5")
        await pilot.pause()
        assert "0 or greater" in message_view._current_text


@pytest.mark.asyncio
async def test_max_tool_calls_per_minute_notes_it_is_ignored_in_local_api_mode(tmp_path: Path):
    screen, _session = _make_screen(
        tmp_path, local_api_gateways=["http://fake-gateway.test/v1"]
    )
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._permission_manager.guardrails.max_tool_calls_per_minute == 0

        screen._handle_command("/max-tool-calls-per-minute 15")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "local-api mode" in message_view._current_text
        assert screen._permission_manager.guardrails.max_tool_calls_per_minute == 0
        reloaded = GuardrailsConfig.load()
        assert reloaded.max_tool_calls_per_minute == 15
