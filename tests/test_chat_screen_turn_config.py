"""Coverage for turn/guardrail-related config commands:
/max-tool-iterations and /artifact-threshold (persisted to config.toml the
same way /timeout persists request_timeout_s, pushed live into the running
AgentLoop) and /max-tool-calls-per-turn / /max-tool-calls-per-minute
(persisted to guardrails.toml's [limits] table, pushed live into the
running PermissionManager's guardrails instance)."""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.paths import config_file
from pcli.config.settings import Settings
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
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


# --- /max-response-tokens ---


@pytest.mark.asyncio
async def test_max_response_tokens_with_no_argument_reports_current_state(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-response-tokens")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "enabled" in message_view._current_text
        assert "512" in message_view._current_text  # Settings' default margin
        assert "no cap (not yet computable)" in message_view._current_text  # no turns yet


@pytest.mark.asyncio
async def test_max_response_tokens_off_then_on(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-response-tokens off")
        await pilot.pause()
        message_view = screen.query_one(MessageView)
        assert "disabled" in message_view._current_text
        assert screen._settings.max_response_tokens_enabled is False
        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["max_response_tokens_enabled"] is False

        screen._handle_command("/max-response-tokens on")
        await pilot.pause()
        assert "enabled" in message_view._current_text
        assert screen._settings.max_response_tokens_enabled is True


@pytest.mark.asyncio
async def test_max_response_tokens_sets_the_safety_margin_and_persists(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/max-response-tokens 2000")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "2,000" in message_view._current_text
        assert screen._settings.max_response_tokens_enabled is True
        assert screen._settings.max_response_tokens_safety_margin == 2000

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["max_response_tokens_safety_margin"] == 2000
        assert raw["max_response_tokens_enabled"] is True


@pytest.mark.asyncio
async def test_max_response_tokens_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/max-response-tokens not-a-number")
        await pilot.pause()
        assert "isn't 'off', 'on', or a valid number" in message_view._current_text

        screen._handle_command("/max-response-tokens -5")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text


@pytest.mark.asyncio
async def test_max_response_tokens_reports_the_currently_computable_cap(tmp_path: Path):
    from pcli.llm.models import Usage
    from pcli.session.models import TurnCost

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        # limit_tokens for "fake-model" falls back to the generic default
        # (128_000, see ContextLimitTable's builtin default).
        session.cost.turns.append(
            TurnCost(
                turn_index=0,
                model="fake-model",
                usage=Usage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
                cost_usd=0.0,
            )
        )

        screen._handle_command("/max-response-tokens")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        expected = 128_000 - 1100 - 512  # default limit - used - default margin
        assert f"{expected:,}" in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_max_response_tokens_is_sent_with_the_next_turns_request(tmp_path: Path):
    """End-to-end: _run_one_turn actually computes and applies the cap
    before running a turn, not just that the command reports it correctly."""
    from pcli.llm.models import Usage
    from pcli.session.models import TurnCost

    screen, session = _make_screen(tmp_path)
    session.cost.turns.append(
        TurnCost(
            turn_index=0,
            model="fake-model",
            usage=Usage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
            cost_usd=0.0,
        )
    )
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )
    )

    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "hello"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        sent = json.loads(route.calls.last.request.content)
        assert sent["max_tokens"] == 128_000 - 1100 - 512


@pytest.mark.asyncio
@respx.mock
async def test_max_response_tokens_disabled_sends_no_cap(tmp_path: Path):
    from pcli.llm.models import Usage
    from pcli.session.models import TurnCost

    screen, session = _make_screen(tmp_path, max_response_tokens_enabled=False)
    session.cost.turns.append(
        TurnCost(
            turn_index=0,
            model="fake-model",
            usage=Usage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
            cost_usd=0.0,
        )
    )
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )
    )

    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "hello"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        sent = json.loads(route.calls.last.request.content)
        assert "max_tokens" not in sent


# --- /temperature ---


@pytest.mark.asyncio
async def test_temperature_sets_persists_and_takes_effect_on_the_live_agent_loop(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._agent_loop is not None
        assert screen._agent_loop._temperature is None

        screen._handle_command("/temperature 0.9")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "0.9" in message_view._current_text
        assert screen._settings.default_temperature == 0.9
        assert screen._agent_loop._temperature == 0.9

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["default_temperature"] == 0.9

        screen._handle_command("/temperature off")
        await pilot.pause()

        assert screen._settings.default_temperature is None
        assert screen._agent_loop._temperature is None
        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert "default_temperature" not in raw
