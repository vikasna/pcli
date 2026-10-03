"""TelegramDaemon: the business logic behind `pcli telegram` - see cli.py's
run_telegram_daemon for the actual python-telegram-bot wiring, which is
thin glue around this class's public methods (not itself heavily unit
tested against a fake PTB - the same division of labor browser/session.py
draws between its own control flow and Playwright's raw calls).

Owns the one ongoing Session for the authorized chat and processes
incoming text messages one at a time through a queue - mirrors
ChatScreen's own "don't run two turns at once" discipline
(_turn_in_progress / queued followups), just as an explicit asyncio.Queue
instead of a Textual worker loop, so a message that arrives mid-turn waits
its turn instead of racing the one already running (both would otherwise
read/mutate the same Session.messages list concurrently).

A fresh Session is started each time the daemon starts (same default `pcli
run` uses with no --session) - restarting `pcli telegram` begins a new
conversation. /new resets it without restarting the daemon.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pcli.agent.headless import new_headless_session, run_headless_task
from pcli.agent.runtime import AgentRuntime
from pcli.config.settings import Settings
from pcli.llm.errors import GatewayError
from pcli.permissions.manager import PermissionManager
from pcli.session.store import SessionStore
from pcli.telegram.permissions import (
    PendingApprovals,
    TelegramSender,
    ask_via_telegram,
    decode_callback_data,
)

logger = logging.getLogger(__name__)


class TelegramDaemon:
    def __init__(
        self,
        *,
        settings: Settings,
        runtime: AgentRuntime,
        permission_manager: PermissionManager,
        store: SessionStore,
        cwd: Path,
        sender: TelegramSender,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._permission_manager = permission_manager
        self._store = store
        self._cwd = cwd
        self._sender = sender
        self._pending_approvals = PendingApprovals()
        self._chat_id = settings.telegram_chat_id
        self._session = new_headless_session(store, settings, cwd)
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    @property
    def session_id(self) -> str:
        return self._session.id

    def _is_authorized(self, chat_id: int) -> bool:
        # Personal automation, not a multi-user bot (see Settings.
        # telegram_chat_id) - anything from another chat is silently
        # ignored, not just refused, so this bot doesn't even confirm to a
        # stranger that it exists and is listening.
        return chat_id == self._chat_id

    async def handle_text(self, chat_id: int, text: str) -> None:
        """Queues a message for processing - never runs it inline. See the
        module docstring for why this matters."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        await self._queue.put(text)

    async def handle_new_command(self, chat_id: int) -> None:
        if not self._is_authorized(chat_id):
            return
        self._session = new_headless_session(self._store, self._settings, self._cwd)
        await self._sender.send_message(chat_id, "Started a new session.")

    async def handle_unsupported_command(self, chat_id: int, command: str) -> None:
        """Telegram has no equivalent of the TUI's full slash-command set
        (/models, /budget, /timeout, ...) - only /new is implemented here.
        Without this, bot.py's handler registration means anything else
        /-prefixed matches no handler at all and is silently dropped by
        python-telegram-bot itself, before TelegramDaemon ever sees it - a
        real reported confusion ("I sent /models and nothing happened").
        Same authorization gate as handle_text/handle_new_command."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        await self._sender.send_message(
            chat_id,
            f"'{command}' isn't a command this Telegram bot supports - only /new is. "
            "Anything else (no leading /) is sent to the agent as a normal message.",
        )

    def handle_callback(self, data: str) -> None:
        """No chat-id check here on purpose: a callback_data payload is
        meaningless (decode_callback_data returns None) unless it matches
        an id this exact process handed out via PendingApprovals.register
        - there's nothing for an unauthorized chat to forge here even if
        it somehow saw the button."""
        decoded = decode_callback_data(data)
        if decoded is None:
            return
        request_id, result = decoded
        self._pending_approvals.resolve(request_id, result)

    async def run_forever(self) -> None:
        """The daemon's single-consumer loop - runs until cancelled (see
        cli.py's shutdown handling). A single bad turn (a bug in a tool, an
        unexpected exception) is caught and reported back to the chat
        rather than killing the whole daemon - a long-running process
        shouldn't die because one message went badly."""
        while True:
            text = await self._queue.get()
            try:
                await self._process(text)
            except Exception:
                logger.exception("Unhandled error processing a Telegram message")
                try:
                    await self._sender.send_message(
                        self._chat_id,
                        "Something went wrong handling that - check pcli's logs.",
                    )
                except Exception:
                    logger.exception("Also failed to report that error back to Telegram")

    async def _process(self, text: str) -> None:
        # A turn can take a while (several tool calls) with nothing else
        # sent back until it finishes - without this, a message mid-turn
        # looks identical to one that was never received at all. One
        # upfront acknowledgment is the right-sized fix for a chat
        # interface; per-tool-call progress would just be spam (see
        # run_headless_task's on_progress, deliberately left unwired here).
        await self._sender.send_message(self._chat_id, "Working on it...")

        async def ask(tool_name: str, arguments: dict, risk_description: str):
            return await ask_via_telegram(
                self._sender,
                self._pending_approvals,
                self._chat_id,
                tool_name,
                arguments,
                risk_description,
            )

        try:
            result = await run_headless_task(
                text,
                session=self._session,
                runtime=self._runtime,
                settings=self._settings,
                permission_manager=self._permission_manager,
                cwd=self._cwd,
                store=self._store,
                ask=ask,
            )
        except GatewayError as exc:
            await self._sender.send_message(self._chat_id, f"Gateway error: {exc.message}")
            return

        await self._sender.send_message(self._chat_id, result.final_text or "(no reply)")
        if result.truncations_exhausted:
            await self._sender.send_message(
                self._chat_id,
                "Response kept getting cut off by the token limit even after retrying - it "
                "may be incomplete. Send another message to continue.",
            )
