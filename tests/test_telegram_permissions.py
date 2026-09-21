"""Coverage for telegram/permissions.py - PendingApprovals' future
correlation, the callback_data encode/decode round-trip, and
ask_via_telegram itself. None of this touches python-telegram-bot at all
(see the module's own docstring) - a small fake TelegramSender stands in
for the real PTB-backed one (telegram/sender.py, covered separately)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pcli.telegram.permissions import (
    PendingApprovals,
    ask_via_telegram,
    decode_callback_data,
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


# --- PendingApprovals ---


@pytest.mark.asyncio
async def test_register_returns_unique_ids_and_an_unresolved_future():
    pending = PendingApprovals()
    id1, future1 = pending.register()
    id2, future2 = pending.register()

    assert id1 != id2
    assert not future1.done()
    assert not future2.done()


@pytest.mark.asyncio
async def test_resolve_sets_the_futures_result_and_returns_true():
    pending = PendingApprovals()
    request_id, future = pending.register()

    assert pending.resolve(request_id, ("allow", "once")) is True
    assert future.done()
    assert future.result() == ("allow", "once")


@pytest.mark.asyncio
async def test_resolve_unknown_id_is_a_harmless_no_op():
    pending = PendingApprovals()
    assert pending.resolve("does-not-exist", ("allow", "once")) is False


@pytest.mark.asyncio
async def test_resolve_twice_only_takes_effect_once():
    pending = PendingApprovals()
    request_id, future = pending.register()

    assert pending.resolve(request_id, ("allow", "once")) is True
    assert pending.resolve(request_id, ("deny", None)) is False
    assert future.result() == ("allow", "once")  # first resolution wins


# --- callback_data encode/decode ---


def test_decode_round_trips_every_button_result(monkeypatch: pytest.MonkeyPatch):
    from pcli.telegram.permissions import _BUTTONS, _encode_callback_data

    request_id = "abcd1234"
    for _label, result in _BUTTONS:
        data = _encode_callback_data(request_id, result)
        decoded = decode_callback_data(data)
        assert decoded == (request_id, result)


@pytest.mark.parametrize(
    "data",
    [
        "not-ours-at-all",
        "perm:onlythree:parts",
        "perm:abcd:maybe:once",  # invalid decision
        "other:abcd:allow:once",  # wrong prefix
    ],
)
def test_decode_returns_none_for_anything_not_ours(data: str):
    assert decode_callback_data(data) is None


# --- ask_via_telegram ---


@pytest.mark.asyncio
async def test_ask_via_telegram_sends_a_prompt_with_four_buttons():
    sender = _FakeSender()
    pending = PendingApprovals()

    async def resolve_soon():
        await asyncio.sleep(0)
        chat_id, text, buttons = sender.sent[0]
        assert chat_id == 12345
        assert "run_shell" in text
        assert "rm -rf" in text
        assert "dangerous" in text
        assert buttons is not None
        assert len(buttons) == 4
        labels = [label for label, _data in buttons]
        assert labels == ["Allow Once", "Allow for Session", "Allow Always", "Deny"]
        # Resolve via the same path the daemon's callback-query handler
        # uses - decode the button's own callback_data, don't reach into
        # PendingApprovals internals.
        _once_label, once_data = buttons[0]
        decoded = decode_callback_data(once_data)
        assert decoded is not None
        request_id, result = decoded
        pending.resolve(request_id, result)

    task = asyncio.create_task(resolve_soon())
    result = await ask_via_telegram(
        sender, pending, 12345, "run_shell", {"command": "rm -rf x"}, "dangerous"
    )
    await task

    assert result == ("allow", "once")


@pytest.mark.asyncio
async def test_ask_via_telegram_returns_deny_when_the_deny_button_is_pressed():
    sender = _FakeSender()
    pending = PendingApprovals()

    async def press_deny():
        await asyncio.sleep(0)
        _chat_id, _text, buttons = sender.sent[0]
        deny_label, deny_data = next(b for b in buttons if b[0] == "Deny")
        assert deny_label == "Deny"
        request_id, result = decode_callback_data(deny_data)
        pending.resolve(request_id, result)

    task = asyncio.create_task(press_deny())
    result = await ask_via_telegram(sender, pending, 1, "run_shell", {}, "")
    await task

    assert result == ("deny", None)


@pytest.mark.asyncio
async def test_ask_via_telegram_truncates_a_very_long_arguments_preview():
    sender = _FakeSender()
    pending = PendingApprovals()

    async def resolve_soon():
        await asyncio.sleep(0)
        _chat_id, text, buttons = sender.sent[0]
        assert "more chars truncated" in text
        request_id, result = decode_callback_data(buttons[0][1])
        pending.resolve(request_id, result)

    task = asyncio.create_task(resolve_soon())
    huge_content = "x" * 5000
    await ask_via_telegram(sender, pending, 1, "write_file", {"content": huge_content}, "")
    await task
