"""Integration coverage for the decision log wired into ChatScreen: a
record_decision tool result must render as a distinct, always-visible
"decision"-role message rather than the generic collapsed-by-default tool
Collapsible, and must land on session.decisions."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App
from textual.widgets import Collapsible

from pcli.agent.loop import ToolResultEvent
from pcli.config.settings import Settings
from pcli.llm.models import ToolCall, ToolCallFunction
from pcli.session.models import Message, Session
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _make_screen(tmp_path: Path) -> tuple[ChatScreen, Session]:
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
async def test_show_decision_notice_renders_decision_message(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = ToolResultEvent(
            tool_call=ToolCall(
                id="call_1",
                function=ToolCallFunction(
                    name="record_decision",
                    arguments=json.dumps(
                        {"decision": "Use httpx", "rationale": "already async elsewhere"}
                    ),
                ),
            ),
            output="Decision recorded: Use httpx",
        )
        screen._show_decision_notice(event)
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "decision"
        assert "Use httpx" in message_view._current_text
        assert "already async elsewhere" in message_view._current_text
        # Not routed through the generic collapsed tool-result rendering.
        assert list(message_view.query(Collapsible)) == []


@pytest.mark.asyncio
@respx.mock
async def test_record_decision_tool_call_renders_as_decision_not_collapsible(tmp_path: Path):
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
                                            "name": "record_decision",
                                            "arguments": json.dumps(
                                                {
                                                    "decision": "Use approach A",
                                                    "rationale": "simpler, already tested",
                                                }
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
        httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]},
            ),
        ),
    ]

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None  # on_mount succeeded

        screen._session.messages.append(Message(role="user", content="pick an approach"))
        screen._stream_response()
        for _ in range(20):
            await pilot.pause()

        assert len(session.decisions) == 1
        assert session.decisions[0].decision == "Use approach A"

        message_view = screen.query_one(MessageView)
        assert list(message_view.query(".message-decision"))
        assert list(message_view.query(Collapsible)) == []
