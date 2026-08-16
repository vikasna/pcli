"""Integration coverage for full-clipboard paste in the main chat input:
Shift+Insert shows a compact "[Pasted N lines]" placeholder while composing,
but the *full* pasted text — not the placeholder — must be what actually
lands in session.messages and gets sent to the model."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual import events
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.paste_input import PasteInput


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


@pytest.mark.asyncio
@respx.mock
async def test_submitting_a_pasted_placeholder_sends_the_full_clipboard_text(
    tmp_path: Path, monkeypatch
):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        )
    )

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None  # on_mount succeeded

        field = screen.query_one(PasteInput)
        assert field.id == "input-box"

        import pcli.tui.widgets.paste_input as paste_module

        clipboard_text = "def add(a, b):\n    return a + b\n\nprint(add(1, 2))"
        monkeypatch.setattr(paste_module.pyperclip, "paste", lambda: clipboard_text)

        field.focus()
        field.action_paste_from_os_clipboard()
        await pilot.pause()
        assert field.value == "[Pasted 4 lines]"

        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        user_messages = [m for m in session.messages if m.role == "user"]
        assert len(user_messages) == 1
        assert user_messages[0].content == clipboard_text
        assert "[Pasted" not in user_messages[0].content


@pytest.mark.asyncio
@respx.mock
async def test_terminal_intercepted_paste_also_sends_the_full_clipboard_text(tmp_path: Path):
    """The path that actually matters in practice: most terminals intercept
    Shift+Insert themselves and deliver it as a bracketed-paste events.Paste
    message, not a literal keypress — this must get the same full-text
    treatment as the pyperclip-based shift+insert binding, not Textual's
    stock first-line-only Input._on_paste."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        )
    )

    screen, session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        field = screen.query_one(PasteInput)
        field.focus()

        clipboard_text = "line one\nline two\nline three"
        field.post_message(events.Paste(text=clipboard_text))
        await pilot.pause()
        assert field.value == "[Pasted 3 lines]"

        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        user_messages = [m for m in session.messages if m.role == "user"]
        assert len(user_messages) == 1
        assert user_messages[0].content == clipboard_text
