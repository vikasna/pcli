"""Coverage for ChatScreen._maybe_detect_context_limit, called from
on_mount right after the GatewayClient is built: skips entirely when a
context-limit entry already exists for the model or the feature is
disabled, persists+applies a successful detection immediately, and shows
a fallback notice pointing at /context-limit when nothing was detected."""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.message_view import MessageView
from pcli.tui.widgets.status_bar import StatusBar


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response_with_usage(text: str) -> httpx.Response:
    # Usage arrives in its own final chunk, after all content deltas -
    # matching real streaming APIs, and avoiding a same-chunk race between
    # the assistant's own message bubble and a system notice added mid-turn
    # by the usage handler (see llm/streaming.py: a usage-bearing chunk
    # yields UsageEvent before any TextDelta from that same chunk).
    return httpx.Response(
        200,
        content=_sse(
            {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ),
    )


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path, *, model: str = "unrecognized-local-model", **settings_overrides):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model=model, gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model=model,
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


def _mock_all_native_probes_404() -> None:
    respx.get("http://fake-gateway.test/api/v0/models").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/props").mock(return_value=httpx.Response(404))
    respx.post("http://fake-gateway.test/api/show").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/model/info").mock(return_value=httpx.Response(404))


@pytest.mark.asyncio
@respx.mock
async def test_successful_detection_persists_and_updates_the_status_bar(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)

    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "unrecognized-local-model", "context_length": 32768}]}
        )
    )
    _mock_all_native_probes_404()

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected context limit" in message_view._current_text
        assert "32,768" in message_view._current_text

        status_bar = screen.query_one(StatusBar)
        assert status_bar.context_limit_tokens == 32768

        raw = tomllib.loads(limits_path.read_text(encoding="utf-8"))
        assert raw["models"]["unrecognized-local-model"] == 32768


@pytest.mark.asyncio
@respx.mock
async def test_failed_detection_shows_a_notice_pointing_at_context_limit_command(tmp_path: Path):
    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "unrecognized-local-model"}]})
    )
    _mock_all_native_probes_404()

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Couldn't auto-detect" in message_view._current_text
        assert "/context-limit" in message_view._current_text


@pytest.mark.asyncio
async def test_skips_probing_entirely_when_model_already_has_an_explicit_entry(tmp_path: Path):
    """gpt-4o* matches a builtin ContextLimitTable pattern - no network call
    should even be attempted (no respx mock registered at all; an
    unexpected request here would raise)."""
    screen, _session = _make_screen(tmp_path, model="gpt-4o-mini")
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text


@pytest.mark.asyncio
async def test_skips_entirely_when_disabled_via_settings(tmp_path: Path):
    screen, _session = _make_screen(tmp_path, context_limit_auto_detect_enabled=False)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text


@pytest.mark.asyncio
async def test_auto_detected_entry_is_still_reprobed(tmp_path: Path, monkeypatch):
    """Core regression case for the staleness bug: a model whose [models]
    entry came from a PREVIOUS auto-detect run (recorded in
    [auto_detected]) must still be re-probed on this startup, since a local
    gateway (LM Studio, Ollama, ...) can have reloaded the same model with a
    smaller context-length setting since the value was cached."""
    import pcli.cost.context as context_module
    from pcli.llm.client import GatewayClient

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)
    context_module.set_model_context_limit("unrecognized-local-model", 262144, auto_detected=True)

    calls: list[str] = []

    async def fake_detect(self, model: str) -> int:
        calls.append(model)
        return 16384

    monkeypatch.setattr(GatewayClient, "detect_context_limit", fake_detect)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert calls == ["unrecognized-local-model"]  # probe DID run

        message_view = screen.query_one(MessageView)
        assert "Auto-detected context limit" in message_view._current_text
        assert "16,384" in message_view._current_text

        status_bar = screen.query_one(StatusBar)
        assert status_bar.context_limit_tokens == 16384

        raw = tomllib.loads(limits_path.read_text(encoding="utf-8"))
        assert raw["models"]["unrecognized-local-model"] == 16384
        assert raw["auto_detected"]["unrecognized-local-model"] is True


@pytest.mark.asyncio
async def test_manual_entry_never_triggers_a_reprobe(tmp_path: Path, monkeypatch):
    """Opposite of the above: a model with a MANUAL (non-auto-detected)
    entry - the /context-limit command's path - must never be re-probed,
    since a human's deliberate correction is sticky forever."""
    import pcli.cost.context as context_module
    from pcli.llm.client import GatewayClient

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)
    context_module.set_model_context_limit("unrecognized-local-model", 8000)  # manual, default auto_detected=False

    calls: list[str] = []

    async def fake_detect(self, model: str) -> int:
        calls.append(model)
        return 16384

    monkeypatch.setattr(GatewayClient, "detect_context_limit", fake_detect)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert calls == []  # probe must NOT run

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text
        assert screen._context_limit_table.lookup("unrecognized-local-model") == 8000


@pytest.mark.asyncio
@respx.mock
async def test_a_prior_manual_context_limit_setting_is_never_overwritten(tmp_path: Path, monkeypatch):
    """Regression guard: has_explicit_entry() must treat a manual
    /context-limit correction (or a previous auto-detection) as final -
    detection must not silently re-probe and clobber it with a different
    value on a later launch."""
    import pcli.cost.context as context_module

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)
    context_module.set_model_context_limit("unrecognized-local-model", 8000)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text
        assert screen._context_limit_table.lookup("unrecognized-local-model") == 8000


@pytest.mark.asyncio
@respx.mock
async def test_retries_detection_once_the_model_has_actually_responded(tmp_path: Path, monkeypatch):
    """Regression coverage for a real gap: a local gateway that JIT-loads
    (LM Studio, in particular) may not report a model's real context size at
    pcli's startup-time probe, since the model isn't loaded into memory yet.
    Once the model actually produces a response, it must be loaded - so the
    turn loop gets exactly one more shot at detection, via the "usage" chunk
    handler in _run_one_turn."""
    models_route = respx.get("http://fake-gateway.test/v1/models")
    models_route.side_effect = [
        # Startup probe: no usable field yet (simulates a not-loaded model).
        httpx.Response(200, json={"data": [{"id": "unrecognized-local-model"}]}),
        # Retry after the first real response: now it resolves.
        httpx.Response(
            200, json={"data": [{"id": "unrecognized-local-model", "context_length": 32768}]}
        ),
    ]
    _mock_all_native_probes_404()
    chat_route = respx.post("http://fake-gateway.test/v1/chat/completions")
    chat_route.side_effect = [
        _text_response_with_usage("first reply"),
        _text_response_with_usage("second reply"),
    ]

    # add_message's own text is only ever held transiently in
    # message_view._current_text (reset to "" on every finish_streaming(),
    # which the turn loop always calls at the end of _run_one_turn) - a spy
    # on add_message itself is what actually survives past the end of a
    # turn. Patched at the class level, before the app (and its on_mount,
    # which fires the startup probe) ever runs - run_test() itself pumps
    # enough of the event loop to complete on_mount before a test gets
    # control back, so an instance-level patch installed after entering
    # run_test() would already have missed the startup message.
    system_messages: list[str] = []
    original_add_message = MessageView.add_message

    def _spy_add_message(self, role, text=""):
        if role == "system":
            system_messages.append(text)
        return original_add_message(self, role, text)

    monkeypatch.setattr(MessageView, "add_message", _spy_add_message)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert any("Couldn't auto-detect" in m for m in system_messages)
        assert screen._context_limit_retry_pending is True

        field = screen.query_one(ChatInput)
        field.focus()
        field.text = "hello"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        assert screen._context_limit_retry_pending is False
        assert any("Auto-detected context limit" in m and "32,768" in m for m in system_messages)
        status_bar = screen.query_one(StatusBar)
        assert status_bar.context_limit_tokens == 32768

        # A second turn must NOT probe again - the standard-/models route
        # only has two responses queued above, so a third call here would
        # raise (StopIteration via respx) rather than silently re-probing.
        field.text = "another message"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        assert screen._context_limit_retry_pending is False
