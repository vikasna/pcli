"""Coverage for telegram/sender.py's BotSender - the real, PTB-backed
TelegramSender. python-telegram-bot is genuinely not installed in this
dev/CI environment (the optional "telegram" extra - see pyproject.toml),
so these tests inject a minimal fake `telegram` module via sys.modules,
same technique test_browser_session.py uses for Playwright - BotSender's
own control flow is what's under test here, not PTB's internals."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from pcli.telegram.sender import _TELEGRAM_MAX_MESSAGE_CHARS, BotSender


class _FakeInlineKeyboardButton:
    def __init__(self, label: str, *, callback_data: str) -> None:
        self.label = label
        self.callback_data = callback_data


class _FakeInlineKeyboardMarkup:
    def __init__(self, rows: list[list[_FakeInlineKeyboardButton]]) -> None:
        self.rows = rows


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str, object]] = []
        self.sent_photos: list[tuple[int, bytes]] = []
        self.sent_documents: list[tuple[int, bytes, str]] = []

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None) -> None:
        self.sent_messages.append((chat_id, text, reply_markup))

    async def send_photo(self, *, chat_id: int, photo: bytes) -> None:
        self.sent_photos.append((chat_id, photo))

    async def send_document(self, *, chat_id: int, document: bytes, filename: str) -> None:
        self.sent_documents.append((chat_id, document, filename))


def _install_fake_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = types.ModuleType("telegram")
    fake_module.InlineKeyboardButton = _FakeInlineKeyboardButton  # type: ignore[attr-defined]
    fake_module.InlineKeyboardMarkup = _FakeInlineKeyboardMarkup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", fake_module)


@pytest.mark.asyncio
async def test_send_message_without_buttons_has_no_reply_markup(monkeypatch: pytest.MonkeyPatch):
    _install_fake_telegram(monkeypatch)
    bot = _FakeBot()
    sender = BotSender(bot)  # type: ignore[arg-type]

    await sender.send_message(123, "hello")

    assert bot.sent_messages == [(123, "hello", None)]


@pytest.mark.asyncio
async def test_send_message_with_buttons_builds_one_row_per_button(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_telegram(monkeypatch)
    bot = _FakeBot()
    sender = BotSender(bot)  # type: ignore[arg-type]

    await sender.send_message(
        123, "approve?", buttons=[("Allow Once", "perm:ab:allow:once"), ("Deny", "perm:ab:deny:-")]
    )

    chat_id, text, reply_markup = bot.sent_messages[0]
    assert chat_id == 123
    assert text == "approve?"
    assert isinstance(reply_markup, _FakeInlineKeyboardMarkup)
    assert len(reply_markup.rows) == 2
    assert [row[0].label for row in reply_markup.rows] == ["Allow Once", "Deny"]
    assert [row[0].callback_data for row in reply_markup.rows] == [
        "perm:ab:allow:once",
        "perm:ab:deny:-",
    ]


@pytest.mark.asyncio
async def test_send_message_truncates_over_the_telegram_length_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_telegram(monkeypatch)
    bot = _FakeBot()
    sender = BotSender(bot)  # type: ignore[arg-type]

    await sender.send_message(1, "x" * (_TELEGRAM_MAX_MESSAGE_CHARS + 500))

    _chat_id, text, _reply_markup = bot.sent_messages[0]
    assert len(text) <= _TELEGRAM_MAX_MESSAGE_CHARS + len("\n... [500 more chars truncated]")
    assert "more chars truncated" in text


@pytest.mark.asyncio
async def test_send_photo_reads_the_file_and_sends_its_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    bot = _FakeBot()
    sender = BotSender(bot)  # type: ignore[arg-type]
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"fake-png-bytes")

    await sender.send_photo(456, image_path)

    assert bot.sent_photos == [(456, b"fake-png-bytes")]


@pytest.mark.asyncio
async def test_send_document_reads_the_file_and_includes_its_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    bot = _FakeBot()
    sender = BotSender(bot)  # type: ignore[arg-type]
    export_path = tmp_path / "abc123.pcli-session.json"
    export_path.write_bytes(b'{"format": "pcli-session"}')

    await sender.send_document(789, export_path)

    assert bot.sent_documents == [(789, b'{"format": "pcli-session"}', "abc123.pcli-session.json")]
