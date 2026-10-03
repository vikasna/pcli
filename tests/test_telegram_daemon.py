"""Coverage for telegram/daemon.py's TelegramDaemon - the business logic
behind `pcli telegram`, exercised end-to-end against a respx-mocked
gateway (the same real run_headless_task/AgentLoop machinery `pcli run`
uses - see test_agent_headless.py), with a fake TelegramSender standing in
for python-telegram-bot (see test_telegram_sender.py for that layer)."""

from __future__ import annotations

import asyncio
import json
import sys
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
@respx.mock
async def test_tool_calls_and_their_results_are_forwarded_to_the_chat(tmp_path: Path):
    """read_file (needs_permission=False) keeps this focused on progress
    forwarding itself, without also exercising the permission-prompt path
    already covered by test_a_permission_requiring_tool_call_round_trips_
    through_the_daemon below."""
    (tmp_path / "notes.txt").write_text("hello from disk", encoding="utf-8")
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
                                            "name": "read_file",
                                            "arguments": json.dumps({"path": "notes.txt"}),
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
        _text_response("Read it."),
    ]
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, "read the notes file")
        await daemon._process(await daemon._queue.get())
    finally:
        await daemon._runtime.client.aclose()

    texts = [text for _chat_id, text, _buttons in sender.sent]
    assert texts[0] == "Working on it..."
    assert texts[-1] == "Read it."
    assert any(line.startswith("  -> read_file(") for line in texts)
    assert any(line.startswith("  <- ") and "hello from disk" in line for line in texts)
    assert all(not line.startswith("> ") for line in texts)  # the task echo is skipped


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
async def test_handle_unsupported_command_explains_the_command_is_not_supported(tmp_path: Path):
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
    assert "isn't a command this Telegram bot supports" in text
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
async def test_handle_text_routes_shell_passthrough_directly_not_through_the_queue(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_text(_AUTHORIZED_CHAT_ID, f'!"{sys.executable}" -c "print(1 + 1)"')
        assert daemon._queue.empty()
    finally:
        await daemon._runtime.client.aclose()

    assert len(sender.sent) == 1
    chat_id, text, buttons = sender.sent[0]
    assert chat_id == _AUTHORIZED_CHAT_ID
    assert "2" in text
    assert "[exit_code=0]" in text
    assert buttons is None


@pytest.mark.asyncio
async def test_handle_shell_passthrough_quiet_variant_hides_output(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        # The expected output ("999001") is deliberately not a substring of
        # the command text that gets echoed back ("999*1000+1") - a literal
        # value (e.g. `print(1 + 1)` -> "2") risks a false positive, since
        # sys.executable's own path can legitimately contain a stray digit
        # (e.g. a "...\Python\3.12.10\..." install dir).
        await daemon.handle_shell_passthrough(
            _AUTHORIZED_CHAT_ID, f'!!"{sys.executable}" -c "print(999*1000+1)"'
        )
    finally:
        await daemon._runtime.client.aclose()

    assert len(sender.sent) == 1
    _chat_id, text, _buttons = sender.sent[0]
    assert "(output hidden)" in text
    assert "999001" not in text


@pytest.mark.asyncio
async def test_handle_shell_passthrough_triple_bang_explains_its_unsupported(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_shell_passthrough(_AUTHORIZED_CHAT_ID, "!!!bash")
    finally:
        await daemon._runtime.client.aclose()

    assert len(sender.sent) == 1
    _chat_id, text, _buttons = sender.sent[0]
    assert "interactive terminal" in text
    assert "isn't supported" in text


@pytest.mark.asyncio
async def test_handle_shell_passthrough_with_no_command_sends_usage(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_shell_passthrough(_AUTHORIZED_CHAT_ID, "!")
    finally:
        await daemon._runtime.client.aclose()

    assert len(sender.sent) == 1
    _chat_id, text, _buttons = sender.sent[0]
    assert "Usage" in text


@pytest.mark.asyncio
async def test_handle_shell_passthrough_from_an_unauthorized_chat_is_ignored(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_shell_passthrough(999, "!echo hi")
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


_SCALAR_SETTING_COMMAND_METHODS = [
    "handle_timeout_command",
    "handle_temperature_command",
    "handle_budget_command",
    "handle_context_limit_command",
    "handle_max_tool_iterations_command",
    "handle_artifact_threshold_command",
    "handle_max_tool_calls_per_turn_command",
    "handle_max_tool_calls_per_minute_command",
    "handle_prune_tool_results_command",
    "handle_max_response_tokens_command",
]


@pytest.mark.asyncio
async def test_scalar_setting_commands_are_ignored_from_an_unauthorized_chat(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        for method_name in _SCALAR_SETTING_COMMAND_METHODS:
            await getattr(daemon, method_name)(999, None)
        assert sender.sent == []
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_timeout_command_views_sets_and_rejects_bad_input(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_timeout_command(_AUTHORIZED_CHAT_ID, None)
        assert "request_timeout_s is currently" in sender.sent[-1][1]

        await daemon.handle_timeout_command(_AUTHORIZED_CHAT_ID, "45")
        assert "request_timeout_s set to 45s" in sender.sent[-1][1]
        assert settings.request_timeout_s == 45.0

        await daemon.handle_timeout_command(_AUTHORIZED_CHAT_ID, "not-a-number")
        assert "isn't a valid number" in sender.sent[-1][1]

        await daemon.handle_timeout_command(_AUTHORIZED_CHAT_ID, "-5")
        assert "must be greater than 0" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_temperature_command_views_sets_clears_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_temperature_command(_AUTHORIZED_CHAT_ID, None)
        assert "default_temperature is currently" in sender.sent[-1][1]

        await daemon.handle_temperature_command(_AUTHORIZED_CHAT_ID, "0.5")
        assert "default_temperature set to 0.5" in sender.sent[-1][1]
        assert settings.default_temperature == 0.5

        await daemon.handle_temperature_command(_AUTHORIZED_CHAT_ID, "off")
        assert "cleared" in sender.sent[-1][1]
        assert settings.default_temperature is None

        await daemon.handle_temperature_command(_AUTHORIZED_CHAT_ID, "nope")
        assert "isn't a valid number" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_budget_command_views_sets_clears_and_rejects_bad_input(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_budget_command(_AUTHORIZED_CHAT_ID, None)
        assert "max_session_cost_usd is currently" in sender.sent[-1][1]

        await daemon.handle_budget_command(_AUTHORIZED_CHAT_ID, "5")
        assert "max_session_cost_usd set to $5.00" in sender.sent[-1][1]
        assert settings.max_session_cost_usd == 5.0

        await daemon.handle_budget_command(_AUTHORIZED_CHAT_ID, "off")
        assert "cleared" in sender.sent[-1][1]
        assert settings.max_session_cost_usd is None

        await daemon.handle_budget_command(_AUTHORIZED_CHAT_ID, "0")
        assert "must be greater than 0" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_context_limit_command_views_sets_and_rejects_bad_input(tmp_path: Path):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_context_limit_command(_AUTHORIZED_CHAT_ID, None)
        assert "Assumed context limit for 'fake-model'" in sender.sent[-1][1]

        await daemon.handle_context_limit_command(_AUTHORIZED_CHAT_ID, "5000")
        assert "Context limit for 'fake-model' set to 5,000 tokens." in sender.sent[-1][1]

        await daemon.handle_context_limit_command(_AUTHORIZED_CHAT_ID, "abc")
        assert "isn't a valid number of tokens" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_max_tool_iterations_command_views_sets_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_max_tool_iterations_command(_AUTHORIZED_CHAT_ID, None)
        assert "max_tool_iterations is currently" in sender.sent[-1][1]

        await daemon.handle_max_tool_iterations_command(_AUTHORIZED_CHAT_ID, "10")
        assert "max_tool_iterations set to 10." in sender.sent[-1][1]
        assert settings.max_tool_iterations == 10

        await daemon.handle_max_tool_iterations_command(_AUTHORIZED_CHAT_ID, "0")
        assert "must be greater than 0" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_artifact_threshold_command_views_sets_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_artifact_threshold_command(_AUTHORIZED_CHAT_ID, None)
        assert "artifact_threshold_chars is currently" in sender.sent[-1][1]

        await daemon.handle_artifact_threshold_command(_AUTHORIZED_CHAT_ID, "500")
        assert "artifact_threshold_chars set to 500." in sender.sent[-1][1]
        assert settings.artifact_threshold_chars == 500

        await daemon.handle_artifact_threshold_command(_AUTHORIZED_CHAT_ID, "-1")
        assert "must be greater than 0" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_max_tool_calls_per_turn_command_views_sets_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_max_tool_calls_per_turn_command(_AUTHORIZED_CHAT_ID, None)
        assert "max_tool_calls_per_turn is currently" in sender.sent[-1][1]

        await daemon.handle_max_tool_calls_per_turn_command(_AUTHORIZED_CHAT_ID, "5")
        assert "max_tool_calls_per_turn set to 5." in sender.sent[-1][1]

        await daemon.handle_max_tool_calls_per_turn_command(_AUTHORIZED_CHAT_ID, "-1")
        assert "must be 0 or greater" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_max_tool_calls_per_minute_command_views_sets_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_max_tool_calls_per_minute_command(_AUTHORIZED_CHAT_ID, None)
        assert "max_tool_calls_per_minute is currently" in sender.sent[-1][1]

        await daemon.handle_max_tool_calls_per_minute_command(_AUTHORIZED_CHAT_ID, "5")
        assert "max_tool_calls_per_minute set to 5." in sender.sent[-1][1]

        await daemon.handle_max_tool_calls_per_minute_command(_AUTHORIZED_CHAT_ID, "-1")
        assert "must be 0 or greater" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_prune_tool_results_command_views_toggles_sets_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_prune_tool_results_command(_AUTHORIZED_CHAT_ID, None)
        assert "prune_tool_results is" in sender.sent[-1][1]

        await daemon.handle_prune_tool_results_command(_AUTHORIZED_CHAT_ID, "off")
        assert "prune_tool_results disabled." in sender.sent[-1][1]
        assert settings.prune_tool_results_enabled is False

        await daemon.handle_prune_tool_results_command(_AUTHORIZED_CHAT_ID, "on")
        assert "prune_tool_results enabled." in sender.sent[-1][1]
        assert settings.prune_tool_results_enabled is True

        await daemon.handle_prune_tool_results_command(_AUTHORIZED_CHAT_ID, "5")
        assert "prune_tool_results_keep_recent_turns set to 5." in sender.sent[-1][1]
        assert settings.prune_tool_results_keep_recent_turns == 5

        await daemon.handle_prune_tool_results_command(_AUTHORIZED_CHAT_ID, "nonsense")
        assert "isn't 'off', 'on', or a valid number" in sender.sent[-1][1]
    finally:
        await daemon._runtime.client.aclose()


@pytest.mark.asyncio
async def test_handle_max_response_tokens_command_views_toggles_sets_and_rejects_bad_input(
    tmp_path: Path,
):
    settings = _settings()
    daemon, sender = await _make_daemon(tmp_path, settings)

    try:
        await daemon.handle_max_response_tokens_command(_AUTHORIZED_CHAT_ID, None)
        assert "max_response_tokens is" in sender.sent[-1][1]

        await daemon.handle_max_response_tokens_command(_AUTHORIZED_CHAT_ID, "off")
        assert "max_response_tokens disabled." in sender.sent[-1][1]
        assert settings.max_response_tokens_enabled is False

        await daemon.handle_max_response_tokens_command(_AUTHORIZED_CHAT_ID, "on")
        assert "max_response_tokens enabled." in sender.sent[-1][1]
        assert settings.max_response_tokens_enabled is True

        await daemon.handle_max_response_tokens_command(_AUTHORIZED_CHAT_ID, "1000")
        assert "max_response_tokens_safety_margin set to 1,000." in sender.sent[-1][1]
        assert settings.max_response_tokens_safety_margin == 1000

        await daemon.handle_max_response_tokens_command(_AUTHORIZED_CHAT_ID, "bogus")
        assert "isn't 'off', 'on', or a valid number" in sender.sent[-1][1]
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
