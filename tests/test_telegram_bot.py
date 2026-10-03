"""Coverage for telegram/bot.py's run_telegram_daemon - the actual
python-telegram-bot wiring. python-telegram-bot is genuinely not installed
in this dev/CI environment (the optional "telegram" extra), so these tests
inject a minimal fake `telegram`/`telegram.ext` module pair via
sys.modules, same technique test_browser_session.py uses for Playwright.

Scope: handler registration, the start/stop lifecycle (including cleanup
of the AgentRuntime's client/browser session), and that an incoming text
update actually reaches TelegramDaemon and produces a reply - not an
exhaustive simulation of PTB's own update-routing/filter-matching, which
this module doesn't reimplement (see the module's own docstring on why
that's out of scope)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Self

import httpx
import pytest
import respx

from pcli.config.settings import Settings
from pcli.telegram.bot import notify_telegram, run_telegram_daemon

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


# --- fake telegram/telegram.ext ---


class _FakeBot:
    last_constructed: _FakeBot | None = None

    def __init__(self, token: str = "") -> None:
        self.token = token
        self.sent_messages: list[tuple[int, str]] = []
        _FakeBot.last_constructed = self

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None) -> None:
        self.sent_messages.append((chat_id, text))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeUpdater:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start_polling(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeApplication:
    def __init__(self, token: str) -> None:
        self.token = token
        self.bot = _FakeBot()
        self.updater = _FakeUpdater()
        self.handlers: list[object] = []
        self.started = False
        self.stopped = False
        self.initialized = False
        self.shutdown_called = False

    def add_handler(self, handler: object) -> None:
        self.handlers.append(handler)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def __aenter__(self) -> Self:
        self.initialized = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.shutdown_called = True


class _FakeApplicationBuilder:
    last_built: _FakeApplication | None = None

    def __init__(self) -> None:
        self._token = ""

    def token(self, token: str) -> _FakeApplicationBuilder:
        self._token = token
        return self

    def build(self) -> _FakeApplication:
        app = _FakeApplication(self._token)
        _FakeApplicationBuilder.last_built = app
        return app


class _FakeCommandHandler:
    def __init__(self, command: str, callback) -> None:
        self.command = command
        self.callback = callback


class _FakeMessageHandler:
    def __init__(self, filters_obj, callback) -> None:
        self.filters = filters_obj
        self.callback = callback


class _FakeCallbackQueryHandler:
    def __init__(self, callback) -> None:
        self.callback = callback


class _FakeFilter:
    def __and__(self, other: object) -> _FakeFilter:
        return self

    def __invert__(self) -> _FakeFilter:
        return self


class _FakeFilters:
    TEXT = _FakeFilter()
    COMMAND = _FakeFilter()


class _FakeContextTypes:
    DEFAULT_TYPE = object


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False
        self.reply_markup_cleared = False

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_reply_markup(self, *, reply_markup=None) -> None:
        self.reply_markup_cleared = True


class _FakeUpdate:
    def __init__(
        self, *, chat_id: int | None = None, text: str | None = None, callback_data: str | None = None
    ) -> None:
        self.effective_chat = _FakeChat(chat_id) if chat_id is not None else None
        self.effective_message = _FakeMessage(text) if text is not None else None
        self.callback_query = _FakeCallbackQuery(callback_data) if callback_data is not None else None


class _FakeInlineKeyboardButton:
    def __init__(self, label: str, *, callback_data: str) -> None:
        self.label = label
        self.callback_data = callback_data


class _FakeInlineKeyboardMarkup:
    def __init__(self, rows: list[list[_FakeInlineKeyboardButton]]) -> None:
        self.rows = rows


def _install_fake_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Update = _FakeUpdate  # type: ignore[attr-defined]
    fake_telegram.Bot = _FakeBot  # type: ignore[attr-defined]
    # BotSender (telegram/sender.py) - a real code path this daemon
    # exercises whenever it actually sends a reply, so the fake `telegram`
    # module needs these too, not just Update.
    fake_telegram.InlineKeyboardButton = _FakeInlineKeyboardButton  # type: ignore[attr-defined]
    fake_telegram.InlineKeyboardMarkup = _FakeInlineKeyboardMarkup  # type: ignore[attr-defined]
    _FakeBot.last_constructed = None

    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.ApplicationBuilder = _FakeApplicationBuilder  # type: ignore[attr-defined]
    fake_ext.CommandHandler = _FakeCommandHandler  # type: ignore[attr-defined]
    fake_ext.MessageHandler = _FakeMessageHandler  # type: ignore[attr-defined]
    fake_ext.CallbackQueryHandler = _FakeCallbackQueryHandler  # type: ignore[attr-defined]
    fake_ext.ContextTypes = _FakeContextTypes  # type: ignore[attr-defined]
    fake_ext.filters = _FakeFilters  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    _FakeApplicationBuilder.last_built = None


@pytest.mark.asyncio
async def test_registers_a_command_a_text_and_a_callback_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()  # returns as soon as it starts polling - see the module docstring

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    assert app.token == "test-token"
    assert len(app.handlers) == 11
    assert any(isinstance(h, _FakeCommandHandler) and h.command == "new" for h in app.handlers)
    assert any(isinstance(h, _FakeCommandHandler) and h.command == "rename" for h in app.handlers)
    assert any(
        isinstance(h, _FakeCommandHandler) and h.command == "allowed_roots" for h in app.handlers
    )
    assert any(isinstance(h, _FakeCommandHandler) and h.command == "memory" for h in app.handlers)
    assert any(isinstance(h, _FakeCommandHandler) and h.command == "help" for h in app.handlers)
    assert any(isinstance(h, _FakeCommandHandler) and h.command == "toolbox" for h in app.handlers)
    assert any(isinstance(h, _FakeCommandHandler) and h.command == "compact" for h in app.handlers)
    assert any(
        isinstance(h, _FakeCommandHandler) and isinstance(h.command, list) and "timeout" in h.command
        for h in app.handlers
    )
    message_handlers = [h for h in app.handlers if isinstance(h, _FakeMessageHandler)]
    assert len(message_handlers) == 2
    # _FakeFilter.__and__/__invert__ are no-ops that just return self (see
    # that class's own docstring-equivalent comment above) - so
    # `filters.TEXT & ~filters.COMMAND` collapses to the literal TEXT
    # instance, and a bare `filters.COMMAND` stays COMMAND. That's enough
    # to tell the two MessageHandlers apart by identity even without real
    # filter-matching logic in the fake.
    assert any(h.filters is _FakeFilters.COMMAND for h in message_handlers)
    assert any(h.filters is _FakeFilters.TEXT for h in message_handlers)
    assert any(isinstance(h, _FakeCallbackQueryHandler) for h in app.handlers)


@pytest.mark.asyncio
async def test_full_lifecycle_starts_and_stops_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()
    ready_ids: list[str] = []

    await run_telegram_daemon(settings, tmp_path, on_ready=ready_ids.append, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    assert app.initialized is True
    assert app.started is True
    assert app.updater.started is True
    assert len(ready_ids) == 1 and ready_ids[0]  # a real session id was passed
    assert app.updater.stopped is True
    assert app.stopped is True
    assert app.shutdown_called is True


@pytest.mark.asyncio
@respx.mock
async def test_an_incoming_text_update_reaches_the_daemon_and_gets_a_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("Hello from the daemon.")
    )
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()

    daemon_task = asyncio.create_task(run_telegram_daemon(settings, tmp_path, stop_event=stop_event))
    try:
        for _ in range(50):
            if _FakeApplicationBuilder.last_built is not None:
                break
            await asyncio.sleep(0.01)
        app = _FakeApplicationBuilder.last_built
        assert app is not None

        text_handler = next(
            h for h in app.handlers
            if isinstance(h, _FakeMessageHandler) and h.filters is _FakeFilters.TEXT
        )
        update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="hi")
        await text_handler.callback(update, context=None)

        for _ in range(100):
            if len(app.bot.sent_messages) >= 2:  # the "Working on it..." ack, then the reply
                break
            await asyncio.sleep(0.01)
        assert app.bot.sent_messages == [
            (_AUTHORIZED_CHAT_ID, "Working on it..."),
            (_AUTHORIZED_CHAT_ID, "Hello from the daemon."),
        ]
    finally:
        stop_event.set()
        await daemon_task


@pytest.mark.asyncio
async def test_an_unsupported_command_update_gets_an_explanatory_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    fallback_handler = next(
        h for h in app.handlers
        if isinstance(h, _FakeMessageHandler) and h.filters is _FakeFilters.COMMAND
    )
    update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="/models")
    await fallback_handler.callback(update, context=None)

    expected_text = (
        "'/models' isn't a command this Telegram bot supports yet. "
        "Anything else (no leading /) is sent to the agent as a normal message."
    )
    assert app.bot.sent_messages == [(_AUTHORIZED_CHAT_ID, expected_text)]


@pytest.mark.asyncio
async def test_a_scalar_setting_command_update_reaches_the_right_daemon_method(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    handler = next(
        h
        for h in app.handlers
        if isinstance(h, _FakeCommandHandler) and isinstance(h.command, list)
    )
    update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="/timeout 45")
    context = types.SimpleNamespace(args=["45"])
    await handler.callback(update, context=context)

    assert app.bot.sent_messages == [
        (_AUTHORIZED_CHAT_ID, "request_timeout_s set to 45s - takes effect on the next gateway request.")
    ]


@pytest.mark.asyncio
async def test_a_scalar_setting_command_with_no_args_reports_the_current_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    handler = next(
        h
        for h in app.handlers
        if isinstance(h, _FakeCommandHandler) and isinstance(h.command, list)
    )
    update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="/timeout")
    context = types.SimpleNamespace(args=[])
    await handler.callback(update, context=context)

    assert len(app.bot.sent_messages) == 1
    assert "request_timeout_s is currently" in app.bot.sent_messages[0][1]


@pytest.mark.asyncio
async def test_a_help_command_update_reaches_the_daemon_and_gets_the_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    handler = next(h for h in app.handlers if isinstance(h, _FakeCommandHandler) and h.command == "help")
    update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="/help")
    await handler.callback(update, context=types.SimpleNamespace(args=[]))

    assert len(app.bot.sent_messages) == 1
    assert "/help" in app.bot.sent_messages[0][1]


@pytest.mark.asyncio
async def test_a_rename_command_update_passes_the_joined_argument_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    handler = next(
        h for h in app.handlers if isinstance(h, _FakeCommandHandler) and h.command == "rename"
    )
    update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="/rename My New Title")
    await handler.callback(update, context=types.SimpleNamespace(args=["My", "New", "Title"]))

    assert app.bot.sent_messages == [(_AUTHORIZED_CHAT_ID, "Session renamed to 'My New Title'.")]


@pytest.mark.asyncio
async def test_a_toolbox_command_update_passes_the_joined_argument_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()
    stop_event.set()

    await run_telegram_daemon(settings, tmp_path, stop_event=stop_event)

    app = _FakeApplicationBuilder.last_built
    assert app is not None
    handler = next(
        h for h in app.handlers if isinstance(h, _FakeCommandHandler) and h.command == "toolbox"
    )
    update = _FakeUpdate(chat_id=_AUTHORIZED_CHAT_ID, text="/toolbox list")
    await handler.callback(update, context=types.SimpleNamespace(args=["list"]))

    assert app.bot.sent_messages == [(_AUTHORIZED_CHAT_ID, "No software discovered yet. Try /toolbox discover <name>.")]


def test_real_ptb_filters_actually_separate_commands_from_plain_text():
    """Regression guard for the actual bug class the fallback handler
    above fixes - every other test in this file uses _FakeFilter, a pure
    no-op stub (__and__/__invert__ both just `return self`, see
    _install_fake_telegram's own comment), which cannot catch a real
    filter-matching regression. This exercises the genuine
    python-telegram-bot filters.COMMAND/filters.TEXT objects instead,
    against a Message carrying a real bot_command MessageEntity - exactly
    how an incoming "/word" update actually looks once Telegram's own
    servers parse it (a bare Message with no entities does NOT match
    filters.COMMAND, confirmed directly - entities are what make this
    filter work, not just a leading "/" in the text). Skips cleanly if the
    optional "telegram" extra isn't installed; this dev environment and
    ci.yml's install step both have it."""
    pytest.importorskip("telegram")
    import datetime

    from telegram import Chat, Message, MessageEntity, Update
    from telegram.ext import filters

    def _command_update(text: str) -> Update:
        entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0]))
        message = Message(
            message_id=1,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=1, type="private"),
            text=text,
            entities=(entity,),
        )
        return Update(update_id=1, message=message)

    models_update = _command_update("/models")
    assert filters.COMMAND.check_update(models_update)
    assert not (filters.TEXT & ~filters.COMMAND).check_update(models_update)

    new_update = _command_update("/new")
    assert filters.COMMAND.check_update(new_update)


@pytest.mark.asyncio
async def test_callback_query_handler_answers_and_clears_the_keyboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()
    stop_event = asyncio.Event()

    daemon_task = asyncio.create_task(run_telegram_daemon(settings, tmp_path, stop_event=stop_event))
    try:
        for _ in range(50):
            if _FakeApplicationBuilder.last_built is not None:
                break
            await asyncio.sleep(0.01)
        app = _FakeApplicationBuilder.last_built
        assert app is not None

        callback_handler = next(h for h in app.handlers if isinstance(h, _FakeCallbackQueryHandler))
        update = _FakeUpdate(callback_data="perm:aaaa:allow:once")
        await callback_handler.callback(update, context=None)

        assert update.callback_query.answered is True
        assert update.callback_query.reply_markup_cleared is True
    finally:
        stop_event.set()
        await daemon_task


# --- notify_telegram (pcli run --notify-telegram) ---


@pytest.mark.asyncio
async def test_notify_telegram_sends_one_message_via_a_standalone_bot(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_telegram(monkeypatch)
    settings = _settings()

    await notify_telegram(settings, "the scheduled run finished")

    bot = _FakeBot.last_constructed
    assert bot is not None
    assert bot.token == "test-token"
    assert bot.sent_messages == [(_AUTHORIZED_CHAT_ID, "the scheduled run finished")]
