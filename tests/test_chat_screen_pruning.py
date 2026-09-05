"""Coverage for /prune-tool-results (view/on/off/set, persisted to
config.toml the same way /timeout persists request_timeout_s) and the
end-to-end wiring of prune_old_tool_results into ChatScreen._run_one_turn,
run right after the turn is saved and before the auto-compact check."""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.paths import config_file
from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.message_view import MessageView


async def _probe_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="not found")


PROBE_TOOL = ToolSpec(
    name="probe_tool",
    description="A deterministic fake tool standing in for a real shell probe, "
    "so these tests don't depend on OS-specific command availability.",
    parameters={
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
    },
    handler=_probe_handler,
    needs_permission=False,
)


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}),
    )


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


# --- /prune-tool-results view/on/off/set ---


@pytest.mark.asyncio
async def test_prune_tool_results_with_no_argument_reports_current_state(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/prune-tool-results")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "enabled" in message_view._current_text
        assert "1" in message_view._current_text  # Settings' default keep_recent_turns


@pytest.mark.asyncio
async def test_prune_tool_results_off_disables_and_persists(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/prune-tool-results off")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "disabled" in message_view._current_text
        assert screen._settings.prune_tool_results_enabled is False

        # False must actually be written, not silently skipped as falsy.
        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["prune_tool_results_enabled"] is False


@pytest.mark.asyncio
async def test_prune_tool_results_on_re_enables(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/prune-tool-results off")
        await pilot.pause()
        screen._handle_command("/prune-tool-results on")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "enabled" in message_view._current_text
        assert screen._settings.prune_tool_results_enabled is True

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["prune_tool_results_enabled"] is True


@pytest.mark.asyncio
async def test_prune_tool_results_sets_keep_recent_turns_and_persists(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/prune-tool-results 3")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "3" in message_view._current_text
        assert screen._settings.prune_tool_results_keep_recent_turns == 3
        assert screen._settings.prune_tool_results_enabled is True  # implicitly re-enabled

        raw = tomllib.loads(config_file().read_text(encoding="utf-8"))
        assert raw["prune_tool_results_keep_recent_turns"] == 3
        assert raw["prune_tool_results_enabled"] is True


@pytest.mark.asyncio
async def test_prune_tool_results_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/prune-tool-results maybe")
        await pilot.pause()
        assert "valid number" in message_view._current_text

        screen._handle_command("/prune-tool-results 0")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text

        screen._handle_command("/prune-tool-results -1")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text


# --- end-to-end wiring into _run_one_turn ---


def _tool_call_response(call_id: str, purpose: str | None = None) -> httpx.Response:
    arguments = {"target": "x"}
    if purpose is not None:
        arguments["purpose"] = purpose
    return httpx.Response(
        200,
        content=_sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {
                                        "name": "probe_tool",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )


@pytest.mark.asyncio
@respx.mock
async def test_old_tool_result_is_pruned_after_enough_turns_pass(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _tool_call_response("call_1", purpose="check for x"),  # turn 1: model calls a tool
        _text_response("not found, moving on"),
        _text_response("turn 2 reply"),  # turn 2: plain text, no tool call
    ]

    screen, session = _make_screen(tmp_path, prune_tool_results_keep_recent_turns=1)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None
        screen._tool_registry.register(PROBE_TOOL)

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "check for x"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        tool_message = next(m for m in session.messages if m.role == "tool")
        assert tool_message.pruned_artifact_id is None  # still within the current turn

        field.text = "next question"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        tool_message = next(m for m in session.messages if m.role == "tool")
        assert tool_message.pruned_artifact_id is not None
        assert "Pruned tool result" in tool_message.content
        assert "Purpose: check for x." in tool_message.content


@pytest.mark.asyncio
@respx.mock
async def test_pruning_disabled_leaves_old_tool_results_untouched(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _tool_call_response("call_1"),
        _text_response("not found"),
        _text_response("turn 2 reply"),
    ]

    screen, session = _make_screen(
        tmp_path, prune_tool_results_enabled=False, prune_tool_results_keep_recent_turns=1
    )
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._tool_registry.register(PROBE_TOOL)

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "check for x"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        field.text = "next question"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        tool_message = next(m for m in session.messages if m.role == "tool")
        assert tool_message.pruned_artifact_id is None
        assert tool_message.content == "not found"
