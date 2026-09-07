"""End-to-end coverage: a real turn where the model calls ask_user_question,
ChatScreen shows AskQuestionModal, and answering it lets the turn continue
with the answer as the tool's result."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App
from textual.widgets import Button

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.ask_question_modal import AskQuestionModal
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


def _text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200, content=_sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})
    )


def _ask_question_tool_call_response(call_id: str, arguments: dict) -> httpx.Response:
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
                                        "name": "ask_user_question",
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
@respx.mock
async def test_ask_user_question_shows_the_modal_and_the_answer_becomes_the_tool_result(
    tmp_path: Path,
):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _ask_question_tool_call_response(
            "call_1", {"question": "Which color?", "options": ["red", "blue"]}
        ),
        _text_response("Thanks, using red."),
    ]

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "pick a color for me"
        await pilot.press("enter")

        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, AskQuestionModal):
                break
        assert isinstance(app.screen, AskQuestionModal)
        modal = app.screen
        assert "Which color?" in modal._question
        assert modal._options == ["red", "blue"]

        red_button = modal.query_one("#ask-question-option-0", Button)
        modal.on_button_pressed(Button.Pressed(red_button))

        for _ in range(20):
            await pilot.pause()

        tool_message = next(m for m in session.messages if m.role == "tool")
        assert tool_message.content == "red"
        assistant_texts = [m.content for m in session.messages if m.role == "assistant" and m.content]
        assert any("Thanks, using red." in text for text in assistant_texts)
