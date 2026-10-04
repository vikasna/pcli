"""BotSender: the real TelegramSender (permissions.py's protocol), backed
by a `telegram.Bot`. python-telegram-bot (the optional "telegram" extra -
see pyproject.toml) is imported lazily inside each method, not at module
level, so pcli imports and runs fine with the extra not installed - same
discipline as browser/session.py's Playwright import, for the same reason:
nothing about constructing a BotSender or importing this module should ever
require the dependency, only actually sending something does.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Bot  # type: ignore[import-not-found]

_TELEGRAM_MAX_MESSAGE_CHARS = 4096
"""Telegram rejects a message body over this length outright - truncated
here rather than left to fail server-side, since a long final answer or
tool-output preview is a completely ordinary case, not an error."""


class BotSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(
        self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None
    ) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if len(text) > _TELEGRAM_MAX_MESSAGE_CHARS:
            omitted = len(text) - _TELEGRAM_MAX_MESSAGE_CHARS
            text = f"{text[:_TELEGRAM_MAX_MESSAGE_CHARS]}\n... [{omitted} more chars truncated]"
        reply_markup = None
        if buttons:
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
            )
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    async def send_photo(self, chat_id: int, path: Any) -> None:
        # Path.read_bytes() (not open()) - PTB's send_photo accepts raw
        # bytes for `photo` directly, and this avoids a blocking open()
        # call sitting in an async function (see ASYNC230).
        await self._bot.send_photo(chat_id=chat_id, photo=Path(path).read_bytes())

    async def send_document(self, chat_id: int, path: Any) -> None:
        # filename= is required here (unlike send_photo above): PTB infers
        # a sensible default for an image, but raw document bytes with no
        # filename show up in the Telegram client as an unnamed/extension-
        # less file - a real usability problem for e.g. a .pcli-session.json
        # export, which only makes sense re-imported under its own name.
        path = Path(path)
        await self._bot.send_document(
            chat_id=chat_id, document=path.read_bytes(), filename=path.name
        )
