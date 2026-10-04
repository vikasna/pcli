"""Coverage for /plan and /build: registry swap (the model never sees a
disallowed tool), status bar tag, and the per-turn prompt reinforcement —
plus a plan-mode-safe tool succeeding vs. a non-safe one being denied."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.agent.prompt import PLAN_MODE_REINFORCEMENT
from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.message_view import MessageView
from pcli.tui.widgets.status_bar import StatusBar


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
async def test_plan_command_enables_plan_mode_and_filters_the_registry(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._agent_loop is not None

        screen._handle_command("/plan")
        await pilot.pause()

        assert screen._plan_mode is True
        status_bar = screen.query_one(StatusBar)
        assert status_bar.plan_mode is True
        assert "PLAN MODE" in status_bar.render()

        message_view = screen.query_one(MessageView)
        assert "Plan mode enabled" in message_view._current_text

        # The registry actually wired into the agent loop is the filtered one.
        active_registry = screen._agent_loop._tool_registry
        assert "read_file" in active_registry
        assert "write_file" not in active_registry


@pytest.mark.asyncio
async def test_build_command_restores_the_full_registry(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        full_registry = screen._tool_registry

        screen._handle_command("/plan")
        await pilot.pause()
        screen._handle_command("/build")
        await pilot.pause()

        assert screen._plan_mode is False
        assert screen.query_one(StatusBar).plan_mode is False
        message_view = screen.query_one(MessageView)
        assert "Build mode restored" in message_view._current_text

        active_registry = screen._agent_loop._tool_registry
        assert len(active_registry) == len(full_registry)
        assert "write_file" in active_registry


@pytest.mark.asyncio
async def test_plan_command_repeated_reports_already_active(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/plan")
        await pilot.pause()
        screen._handle_command("/plan")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Already in plan mode" in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_plan_mode_injects_ephemeral_reinforcement_not_persisted_to_session(tmp_path: Path):
    captured_requests: list[bytes] = []

    def _capture(request):
        captured_requests.append(request.content)
        return _text_response("ok")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=_capture)

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/plan")
        await pilot.pause()

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "look around"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        sent = json.loads(captured_requests[0])
        sent_contents = [m.get("content", "") for m in sent["messages"]]
        assert any(PLAN_MODE_REINFORCEMENT in c for c in sent_contents if c)

        # Never persisted to the actual session.
        assert not any(
            m.content and PLAN_MODE_REINFORCEMENT in m.content for m in session.messages
        )


@pytest.mark.asyncio
@respx.mock
async def test_plan_mode_denies_a_non_safe_tool_call(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps(
                                                {"path": "x.txt", "content": "hi"}
                                            ),
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
        ),
        _text_response("understood"),
    ]

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Force the tool into the active registry to prove the dispatch-time
        # backstop (not just registry filtering) is what denies it.
        assert screen._agent_loop is not None
        from pcli.tools.builtin.fs_tools import WRITE_FILE

        forced_registry = screen._tool_registry.filtered(lambda t: t.plan_mode_safe)
        forced_registry.register(WRITE_FILE)
        screen._plan_mode = True
        screen.query_one(StatusBar).plan_mode = True
        screen._agent_loop.set_tool_registry(forced_registry)

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "create a file"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        tool_message = next(m for m in session.messages if m.role == "tool")
        assert "not available in plan mode" in tool_message.content
        assert not (tmp_path / "x.txt").exists()
