"""Coverage for StatusBar.main_tool_calls - the main agent's own top-level
tool-call count for the current turn, wired into ChatScreen._run_one_turn:
reset to 0 at the start of a turn, incremented on each of the main loop's
own tool_result chunks (see status_bar.py's own docstring for why a
subagent's internal calls, tracked separately via ActivityTracker, must
never be counted here)."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.status_bar import StatusBar


async def _probe_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="ok")


PROBE_TOOL = ToolSpec(
    name="probe_tool",
    description="A deterministic fake tool for scripting a multi-tool-call turn.",
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


def _tool_call_response(call_id: str) -> httpx.Response:
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
                                        "arguments": json.dumps({"target": "x"}),
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


def _make_screen(tmp_path: Path) -> tuple[ChatScreen, SessionStore]:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, store


@pytest.mark.asyncio
@respx.mock
async def test_main_tool_calls_counts_the_current_turns_own_tool_calls_and_resets_next_turn(
    tmp_path: Path,
):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _tool_call_response("call_1"),  # turn 1: two tool calls, then a reply
        _tool_call_response("call_2"),
        _text_response("done"),
        _text_response("no tools this time"),  # turn 2: plain text, no tool call
    ]

    screen, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None
        screen._tool_registry.register(PROBE_TOOL)

        status_bar = screen.query_one(StatusBar)
        assert status_bar.main_tool_calls == 0

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "do the thing twice"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        assert status_bar.main_tool_calls == 2
        # guardrails.max_tool_calls_per_turn's default - the "N" half of the
        # status bar's "n/N" display (see status_bar.py's _render_line1).
        assert status_bar.main_tool_calls_limit == 25

        field.text = "just a question"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        # A fresh turn with no tool calls resets the count, not just leaves
        # the previous turn's count sitting there stale.
        assert status_bar.main_tool_calls == 0
