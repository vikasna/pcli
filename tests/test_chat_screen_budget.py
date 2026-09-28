"""Integration coverage for hard session cost-budget enforcement
(Settings.max_session_cost_usd, ChatScreen._run_one_turn's budget_check
closure passed into AgentLoop.run_turn) - unit coverage for the underlying
pieces (cost_budget_reason, AgentLoop's own budget_check handling) lives in
test_cost_tracker.py and test_agent_loop.py respectively; this covers the
real end-to-end wiring through a live ChatScreen turn."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.paths import pricing_file
from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str, *, usage: dict | None = None) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return httpx.Response(200, content=_sse(*chunks))


async def _probe_handler(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="ok")


PROBE_TOOL = ToolSpec(
    name="probe_tool",
    description="A deterministic fake tool for scripting a multi-round-trip turn.",
    parameters={"type": "object", "properties": {}},
    handler=_probe_handler,
    needs_permission=False,
)


def _tool_call_response(call_id: str, *, usage: dict | None = None) -> httpx.Response:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {"name": "probe_tool", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return httpx.Response(200, content=_sse(*chunks))


def _make_screen(tmp_path: Path, **settings_overrides) -> tuple[ChatScreen, SessionStore]:
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
    return screen, store


@pytest.mark.asyncio
@respx.mock
async def test_turn_stops_immediately_when_already_over_budget(tmp_path: Path):
    """The budget check runs before the very first gateway call of a turn,
    not just on later round-trips - a session already over budget before
    the user even submits must never call the gateway at all."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.mock(return_value=_text_response("should never be seen"))

    screen, _store = _make_screen(tmp_path, max_session_cost_usd=1.0)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None
        screen._session.cost.session_total_usd = 1.50  # already over the $1.00 cap

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "do something"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        assert route.call_count == 0
        # finish_streaming() clears MessageView._current_text once the turn
        # ends, so check the persisted history instead - the same note text
        # AgentLoop.run_turn appended as a real assistant message.
        last_message = screen._session.messages[-1]
        assert last_message.content is not None
        assert "Reached the session cost budget" in last_message.content
        assert "$1.00" in last_message.content


@pytest.mark.asyncio
@respx.mock
async def test_turn_stops_mid_turn_once_a_round_trip_pushes_spend_over_budget(tmp_path: Path):
    """budget_check is consulted at the top of every internal round-trip,
    not just once per turn - a tool-call-heavy turn that crosses the cap
    partway through must stop before its next round-trip, not run to
    completion first."""
    # $1,000,000 per 1M tokens = $1/token, so a handful of tokens is enough
    # to blow well past a $1.00 cap without needing huge fake usage numbers.
    pricing_file().write_text(
        '[default]\ninput_per_1m = 1000000.0\noutput_per_1m = 1000000.0\n', encoding="utf-8"
    )

    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _tool_call_response("call_1", usage={"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2}),
        _text_response("should never be reached"),
    ]

    screen, _store = _make_screen(tmp_path, max_session_cost_usd=1.0)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None
        screen._tool_registry.register(PROBE_TOOL)

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "do the thing"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        # Only the first round-trip happened (its usage pushed spend to
        # $2.00, over the $1.00 cap) - the second scripted response was
        # never requested.
        assert route.call_count == 1
        last_message = screen._session.messages[-1]
        assert last_message.content is not None
        assert "Reached the session cost budget" in last_message.content
        assert screen._session.cost.session_total_usd == pytest.approx(2.0)


@pytest.mark.asyncio
@respx.mock
async def test_turn_proceeds_normally_when_under_budget(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("the answer")
    )

    screen, _store = _make_screen(tmp_path, max_session_cost_usd=100.0)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "say hello"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        assert any(m.content == "the answer" for m in screen._session.messages)
        assert not any(
            m.content and "Reached the session cost budget" in m.content
            for m in screen._session.messages
        )


@pytest.mark.asyncio
@respx.mock
async def test_no_budget_configured_never_stops_the_turn(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("the answer")
    )

    screen, _store = _make_screen(tmp_path)  # max_session_cost_usd unset (default)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None
        screen._session.cost.session_total_usd = 999_999.0  # would fail any check with a cap

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "say hello"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        assert any(m.content == "the answer" for m in screen._session.messages)
