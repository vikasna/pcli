"""Integration coverage for surfacing reasoning/"thinking" model output in
the TUI: previously delta.reasoning_content (streamed by reasoning models
like Nemotron "detailed thinking" served through LM Studio/vLLM) was
dropped entirely by the SSE parser, and a model that spent its whole
response reasoning without ever producing content or a tool call left a
literally empty assistant bubble with no explanation anywhere - looking
exactly like pcli had silently failed to do anything."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App
from textual.widgets import Collapsible

from pcli.config.settings import Settings
from pcli.session.models import Message
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


def _reasoning_collapsibles(screen: ChatScreen) -> list[Collapsible]:
    return [
        w
        for w in screen.query(Collapsible)
        if "reasoning-collapsible" in (w.classes or ())
    ]


@pytest.mark.asyncio
@respx.mock
async def test_reasoning_followed_by_real_content_shows_both(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "hmm, let's see"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "The answer is 42."}, "finish_reason": "stop"}]},
            ),
        )
    )

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        screen._session.messages.append(Message(role="user", content="what is it"))
        screen._stream_response()
        for _ in range(10):
            await pilot.pause()

        reasoning = _reasoning_collapsibles(screen)
        assert len(reasoning) == 1
        assert reasoning[0].collapsed is True  # collapsed by default, not intrusive

        message_view = screen.query_one(MessageView)
        assert "The answer is 42." in message_view._current_text or any(
            m.role == "assistant" and m.content == "The answer is 42." for m in session.messages
        )
        assistant_messages = [m for m in session.messages if m.role == "assistant"]
        assert assistant_messages[-1].content == "The answer is 42."
        # The reasoning trace must never leak into the persisted message
        # content that gets replayed back to the model on the next turn.
        assert "hmm" not in (assistant_messages[-1].content or "")


@pytest.mark.asyncio
@respx.mock
async def test_reasoning_only_response_shows_notice_and_does_not_look_silently_broken(
    tmp_path: Path,
):
    """The actual bug this fixes: a model that spends its whole response
    reasoning and never produces content or a tool call must not look like
    pcli did nothing - a clear system notice should appear."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "thinking a lot and not answering"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
        )
    )

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        screen._session.messages.append(Message(role="user", content="write the report"))
        screen._stream_response()
        for _ in range(10):
            await pilot.pause()

        reasoning = _reasoning_collapsibles(screen)
        assert len(reasoning) == 1

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "didn't produce a reply" in message_view._current_text
        assert "Thinking" in message_view._current_text

        # Still saved, so the failed attempt isn't lost.
        reloaded_assistant = [m for m in session.messages if m.role == "assistant"]
        assert reloaded_assistant  # the (contentless) assistant message is still recorded


@pytest.mark.asyncio
@respx.mock
async def test_truly_empty_response_with_no_reasoning_still_shows_notice(tmp_path: Path):
    """No reasoning either - the fallback notice must not depend on
    reasoning having been present, since some backend could plausibly
    return an empty response with no reasoning at all."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
    )

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        screen._session.messages.append(Message(role="user", content="hello"))
        screen._stream_response()
        for _ in range(10):
            await pilot.pause()

        assert _reasoning_collapsibles(screen) == []

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "didn't produce a reply" in message_view._current_text
        assert "Thinking" not in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_reasoning_before_a_tool_call_is_flushed_and_turn_completes_normally(
    tmp_path: Path,
):
    """A tool call counts as real content too - the fallback notice must
    not fire, and the reasoning that preceded the tool call still renders."""
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "I should check the file"}, "finish_reason": None}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "list_dir", "arguments": '{"path": "."}'},
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
            content=_sse({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}),
        ),
    ]

    screen, _session = _make_screen(tmp_path)
    # Nothing calls a real tool named list_dir in this bare screen fixture,
    # so it will resolve as "Unknown tool" - that's fine, this test only
    # cares about the reasoning/notice behavior, not tool dispatch.
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        screen._session.messages.append(Message(role="user", content="look around"))
        screen._stream_response()
        for _ in range(15):
            await pilot.pause()

        assert len(_reasoning_collapsibles(screen)) == 1

        message_view = screen.query_one(MessageView)
        assert message_view._current_role != "system" or "didn't produce a reply" not in (
            message_view._current_text or ""
        )
