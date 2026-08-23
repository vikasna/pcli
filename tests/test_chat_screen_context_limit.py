"""Coverage for the context-ceiling detection notice and the /context-limit
command it points at.

Regression coverage for a real debugged session: a model with no
ContextLimitTable entry (silently falling back to a generic 128000-token
guess) produced consecutive empty turns with total_tokens plateaued near
its real, much smaller window, while pcli's own fraction-based
auto-compact check thought the session was nowhere near full and never
triggered. The empty-turn notice now detects this pattern and points the
user at /context-limit to correct the assumed limit."""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.llm.models import Usage
from pcli.session.models import TurnCost
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


def _turn(index: int, prompt: int, completion: int) -> TurnCost:
    return TurnCost(
        turn_index=index,
        model="fake-model",
        usage=Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion),
        cost_usd=0.0,
    )


@pytest.mark.asyncio
@respx.mock
async def test_empty_turn_with_plateaued_usage_shows_context_ceiling_notice(tmp_path: Path):
    screen, session = _make_screen(tmp_path)
    # Prior turn: real content, prompt/total climbing normally.
    session.cost.turns.append(_turn(0, 14984, 1377))

    # This turn: empty (no content, no tool call), total plateaued near the
    # previous turn's despite prompt growing further - the ceiling signature.
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 15414, "completion_tokens": 970, "total_tokens": 16384},
                }
            ),
        )
    )

    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        from pcli.tui.widgets.paste_input import PasteInput

        field = screen.query_one(PasteInput)
        field.focus()
        field.value = "continue"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "didn't produce a reply" in message_view._current_text
        assert "context limit" in message_view._current_text
        assert "/context-limit" in message_view._current_text
        assert "/compact" in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_empty_turn_without_plateau_shows_generic_notice(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    # No prior turn history at all - looks_like_context_ceiling requires >= 2
    # turns to compare, so this must fall back to the plain notice.
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
    )

    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        from pcli.tui.widgets.paste_input import PasteInput

        field = screen.query_one(PasteInput)
        field.focus()
        field.value = "hello"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "didn't produce a reply" in message_view._current_text
        assert "context limit" not in message_view._current_text
        assert "Try again" in message_view._current_text


# --- /context-limit ---


@pytest.mark.asyncio
async def test_context_limit_command_with_no_argument_reports_current_value(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/context-limit")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "128,000" in message_view._current_text  # the generic default
        assert "fake-model" in message_view._current_text


@pytest.mark.asyncio
async def test_context_limit_command_sets_and_persists(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        screen._handle_command("/context-limit 16384")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "16,384" in message_view._current_text

        # Persisted to disk...
        raw = tomllib.loads(limits_path.read_text(encoding="utf-8"))
        assert raw["models"]["fake-model"] == 16384

        # ...and takes effect immediately, no restart - the in-memory table
        # used for the actual auto-compact check was reloaded.
        assert screen._context_limit_table.lookup("fake-model") == 16384


@pytest.mark.asyncio
async def test_context_limit_command_rejects_invalid_input(tmp_path: Path):
    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        message_view = screen.query_one(MessageView)

        screen._handle_command("/context-limit not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text

        screen._handle_command("/context-limit -5")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text
