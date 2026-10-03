"""run_telegram_daemon: the actual python-telegram-bot wiring behind `pcli
telegram` (cli.py) - builds an Application, registers handlers that
forward to TelegramDaemon's public methods (daemon.py, where the real
business logic and its test coverage live), and runs until stopped.

python-telegram-bot (the optional "telegram" extra - see pyproject.toml)
is imported lazily inside this function, not at module level, so pcli
imports and runs fine with the extra not installed - same discipline as
browser/session.py's Playwright import and telegram/sender.py's own.

This module is deliberately thin glue around TelegramDaemon, in the same
spirit browser/session.py's _ensure_page is thin glue around Playwright's
own launch call - the business logic it wires up (message queueing,
authorization, permission correlation) is what's heavily unit tested
(test_telegram_daemon.py, test_telegram_permissions.py); this function's
own handler-registration/lifecycle wiring gets lighter, fake-PTB coverage
(test_telegram_bot.py) rather than an exhaustive one, since faithfully
reproducing PTB's full Application/Updater lifecycle in a fake would cost
far more than it would ever catch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from pcli.agent.runtime import build_agent_runtime, build_permission_manager
from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.telegram.daemon import TelegramDaemon
from pcli.telegram.sender import BotSender

logger = logging.getLogger(__name__)


async def notify_telegram(settings: Settings, text: str) -> None:
    """A one-off message to the configured chat, independent of the full
    daemon above - what `pcli run --notify-telegram` uses to report a
    scheduled run's result without needing a whole Application/polling
    loop just to send one outgoing message. Callers check
    settings.is_telegram_configured() themselves first (see cli.py) - this
    assumes both fields are already set."""
    from telegram import Bot  # type: ignore[import-not-found]

    async with Bot(settings.telegram_bot_token) as bot:
        await BotSender(bot).send_message(settings.telegram_chat_id, text)


async def run_telegram_daemon(
    settings: Settings,
    cwd: Path,
    *,
    on_ready: Callable[[str], None] = lambda _session_id: None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Runs until `stop_event` is set (or the process receives Ctrl+C,
    propagating as KeyboardInterrupt/CancelledError through the wait
    below) - a fresh, internal Event by default, so a plain `await
    run_telegram_daemon(settings, cwd)` in cli.py blocks forever, exactly
    what a long-running daemon should do. Tests pass an already-set Event
    to make this return promptly instead.

    `on_ready(session_id)` fires once polling has actually started -
    cli.py uses it to print a confirmation with something to reference
    (`pcli --resume <session_id>` after the daemon stops)."""
    from telegram import Update  # type: ignore[import-not-found]
    from telegram.ext import (  # type: ignore[import-not-found]
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    runtime = await build_agent_runtime(settings, cwd, browser_headless=True)
    store = SessionStore()
    permission_manager = build_permission_manager(settings)

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    sender = BotSender(application.bot)
    daemon = TelegramDaemon(
        settings=settings,
        runtime=runtime,
        permission_manager=permission_manager,
        store=store,
        cwd=cwd,
        sender=sender,
    )

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is not None and message is not None and message.text:
            await daemon.handle_text(chat.id, message.text)

    async def on_new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is not None:
            await daemon.handle_new_command(update.effective_chat.id)

    async def on_rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        arg = " ".join(context.args) if context.args else None
        await daemon.handle_rename_command(chat.id, arg)

    async def on_allowed_roots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        rest = " ".join(context.args) if context.args else ""
        await daemon.handle_allowed_roots_command(chat.id, rest)

    async def on_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        rest = " ".join(context.args) if context.args else ""
        await daemon.handle_memory_command(chat.id, rest)

    async def on_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is not None:
            await daemon.handle_help_command(update.effective_chat.id)

    async def on_toolbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        rest = " ".join(context.args) if context.args else ""
        await daemon.handle_toolbox_command(chat.id, rest)

    async def on_unsupported_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or not message.text:
            return
        command = message.text.split()[0] if message.text.split() else message.text
        await daemon.handle_unsupported_command(chat.id, command)

    # Telegram bot commands can only contain [A-Za-z0-9_] - no hyphens - so
    # every hyphenated TUI command name below is spelled with underscores
    # here instead (e.g. /context-limit -> /context_limit). A single
    # CommandHandler registered against all these names, routed by this one
    # dict, instead of ten near-identical PTB handler registrations.
    scalar_setting_commands: dict[str, Callable[[int, str | None], Awaitable[None]]] = {
        "timeout": daemon.handle_timeout_command,
        "temperature": daemon.handle_temperature_command,
        "budget": daemon.handle_budget_command,
        "context_limit": daemon.handle_context_limit_command,
        "max_tool_iterations": daemon.handle_max_tool_iterations_command,
        "artifact_threshold": daemon.handle_artifact_threshold_command,
        "max_tool_calls_per_turn": daemon.handle_max_tool_calls_per_turn_command,
        "max_tool_calls_per_minute": daemon.handle_max_tool_calls_per_minute_command,
        "prune_tool_results": daemon.handle_prune_tool_results_command,
        "max_response_tokens": daemon.handle_max_response_tokens_command,
    }

    async def on_scalar_setting_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or not message.text:
            return
        tokens = message.text.split()
        if not tokens:
            return
        command = tokens[0][1:].partition("@")[0]  # drop leading "/" and any "@botname" suffix
        handler = scalar_setting_commands.get(command)
        if handler is None:
            return
        arg = " ".join(context.args) if context.args else None
        await handler(chat.id, arg)

    async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if query.data:
            daemon.handle_callback(query.data)
        try:
            # Best-effort - clears the buttons so a decision can't be
            # pressed twice. Not fatal if the message is too old/already
            # edited for Telegram to allow this.
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not clear the inline keyboard after a decision", exc_info=True)

    application.add_handler(CommandHandler("new", on_new_command))
    application.add_handler(CommandHandler("rename", on_rename_command))
    application.add_handler(CommandHandler("allowed_roots", on_allowed_roots_command))
    application.add_handler(CommandHandler("memory", on_memory_command))
    application.add_handler(CommandHandler("help", on_help_command))
    application.add_handler(CommandHandler("toolbox", on_toolbox_command))
    application.add_handler(
        CommandHandler(list(scalar_setting_commands), on_scalar_setting_command)
    )
    # Must come before the plain-text handler below and after the specific
    # command handlers above: filters.COMMAND matches any /word-shaped
    # message, not just registered ones, so without this anything not
    # explicitly registered matched no handler at all and was silently
    # dropped by PTB itself - never even reaching TelegramDaemon. See
    # handle_unsupported_command's own docstring.
    application.add_handler(MessageHandler(filters.COMMAND, on_unsupported_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(CallbackQueryHandler(on_callback_query))

    worker = asyncio.create_task(daemon.run_forever())
    try:
        async with application:
            await application.start()
            await application.updater.start_polling()
            on_ready(daemon.session_id)
            wait_for = stop_event if stop_event is not None else asyncio.Event()
            try:
                await wait_for.wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await application.updater.stop()
                await application.stop()
    finally:
        worker.cancel()
        await runtime.client.aclose()
        await runtime.browser_session.close()
