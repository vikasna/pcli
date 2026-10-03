"""Coverage for telegram/daemon.py's TelegramDaemon - the business logic
behind `pcli telegram`, exercised end-to-end against a respx-mocked
gateway (the same real run_headless_task/AgentLoop machinery `pcli run`
uses - see test_agent_headless.py), with a fake TelegramSender standing in
for python-telegram-bot (see test_telegram_sender.py for that layer)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pcli.agent.runtime import build_agent_runtime, build_permission_manager
from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.telegram.daemon import TelegramDaemon
from pcli.telegram.permissions import decode_callback_data

_AUTHORIZED_CHAT_ID = 555


def _settings(**overrides) -> Settings:
    defaults = {
        "gateway_base_url": "http://fake-gateway.test/v1",
        "gateway_api_key": "test-key",
        "default_model": "fake-model",
        "sandbox_backend": "subprocess",
        "telegram_bot_token": "test-token",
        "telegram_chat_id": _AUTHORIZED_CHAT_ID,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(
            {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
        ),
    )


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list[tuple[str, str]] | None]] = []

    async def send_message(
        self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None
    ) -> None:
        self.sent.append((chat_id, text, buttons))

    async def send_photo(self, chat_id: int, path: Any) -> None:
        raise AssertionError("not used in these tests")


async def _make_daemon(tmp_path: Path, settings: Settings) -> tuple[TelegramDaemon, _FakeSender]:
    store = SessionStore(base_dir=tmp_path / "sessions")
    runtime = await build_agent_runtime(settings, tmp_path)
    sender = _FakeSender()
    daemon = TelegramDaemon(
        settings=settings,
        runtime=runtime,
        permission_manager=build_permission_manager(settings),
        store=store,
        cwd=tmp_path,
        sender=sender,
    )
    return daemon, sender


@pytest.mark.asyncio
@respx.mock
async def test_handle_text_processes_a_message_and_replies(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("Hello there.")
    )
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "hi")
        await daemon._process(await daemon._queue.get())
    finally:
        await daemon._runtime.client.aclose()

    assert sender.sent == [
        (_AUTHORIZED_CHAT_ID, "Working on it...", None),
        (_AUTHORIZED_CHAT_ID, "Hello there.", None),
    ]


@pytest.mark.asyncio
async def test_handle_text_from_an_unauthorized_chat_is_ignored(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_text(999, "hi")
        assert daemon._queue.empty()
        assert sender.sent == []
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_new_command_starts_a_fresh_session_and_confirms(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        original_id = daemon.session_id
        await daemon.handle_new_command(_AUTHORIZED_CHAT_ID)

        assert daemon.session_id != original_id
        assert sender.sent == [(_AUTHORIZED_CHAT_ID, "Started a new session.", None)]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_unsupported_command_explains_only_new_is_supported(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_unsupported_command(_AUTHORIZED_CHAT_ID, "/models")
    finally:
        await daemon._runtime.client.aclose()

    assert len(sender.sent) == 1
    chat_id, text, buttons = sender.sent[0]
    assert chat_id == _AUTHORIZED_CHAT_ID
    assert "/models" in text
    assert "/new" in text
    assert buttons is None


@pytest.mark.asyncio
async def test_handle_unsupported_command_from_an_unauthorized_chat_is_ignored(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_unsupported_command(999, "/models")
        assert sender.sent == []
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_new_command_from_an_unauthorized_chat_is_ignored(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        original_id = daemon.session_id
        await daemon.handle_new_command(999)

        assert daemon.session_id == original_id
        assert sender.sent == []
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_a_permission_requiring_tool_call_round_trips_through_the_daemon(tmp_path: Path):
    """End-to-end: a run_shell tool call triggers ask_via_telegram, which
    sends an inline-keyboard prompt; handle_callback (what the daemon's
    real callback-query handler calls, per cli.py) resolves it exactly as
    a button press would, and the turn continues using that decision."""
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
                                            "name": "run_shell",
                                            "arguments": json.dumps({"command": "echo hi"}),
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
        _text_response("Ran it."),
    ]
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    async def press_allow_once_soon() -> None:
        # Waits specifically for the permission prompt (the message with
        # buttons), not just any message - the "Working on it..."
        # acknowledgment is sent first and has no buttons.
        while not sender.sent or sender.sent[-1][2] is None:
            await asyncio.sleep(0)
        _chat_id, text, buttons = sender.sent[-1]
        assert "run_shell" in text
        assert buttons is not None
        _label, callback_data = next(b for b in buttons if b[0] == "Allow Once")
        daemon.handle_callback(callback_data)

    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "run a command")
        pressed = asyncio.create_task(press_allow_once_soon())
        await daemon._process(await daemon._queue.get())
        await pressed
    finally:
        await daemon._runtime.client.aclose()

    assert sender.sent[-1] == (_AUTHORIZED_CHAT_ID, "Ran it.", None)


@pytest.mark.asyncio
async def test_handle_callback_with_unrelated_data_is_a_no_op(tmp_path: Path):
    settings = _settings()
    daemon, _sender = await _make_daemon(tmp_path, settings)
    try:
        daemon.handle_callback("not-one-of-ours")  # must not raise
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_gateway_error_is_reported_to_the_chat_not_raised(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("the read operation timed out")
    )
    settings = _settings(max_retries=1)
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "hi")
        await daemon._process(await daemon._queue.get())  # must not raise
    finally:
        await daemon._runtime.client.aclose()

    assert len(sender.sent) == 2  # the "Working on it..." ack, then the error
    chat_id, text, _buttons = sender.sent[-1]
    assert chat_id == _AUTHORIZED_CHAT_ID
    assert "Gateway error" in text


@pytest.mark.asyncio
@respx.mock
async def test_run_forever_processes_queued_messages_one_at_a_time_in_order(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [_text_response("first reply"), _text_response("second reply")]
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    worker = asyncio.create_task(daemon.run_forever())
    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "first")
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "second")
        for _ in range(50):
            if len(sender.sent) >= 4:  # ack + reply, twice
                break
            await asyncio.sleep(0.01)
    finally:
        worker.cancel()
        await daemon._runtime.client.aclose()

    assert [text for _chat_id, text, _buttons in sender.sent] == [
        "Working on it...",
        "first reply",
        "Working on it...",
        "second reply",
    ]


@pytest.mark.asyncio
async def test_run_forever_survives_an_unexpected_exception_in_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    calls = 0

    async def _boom(text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(daemon, "_process", _boom)

    worker = asyncio.create_task(daemon.run_forever())
    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "first")
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "second")
        for _ in range(50):
            if calls >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        worker.cancel()
        await daemon._runtime.client.aclose()

    assert calls == 2  # the second message was still processed after the first one blew up
    assert any("went wrong" in text for _chat_id, text, _buttons in sender.sent)


def test_decode_callback_data_is_reachable_from_handle_callback():
    """Sanity check that daemon.handle_callback and ask_via_telegram agree
    on the same wire format (both go through decode_callback_data) - a
    trivial import-level check, not a behavior test."""
    assert decode_callback_data("perm:aaaa:allow:once") == ("aaaa", ("allow", "once"))
