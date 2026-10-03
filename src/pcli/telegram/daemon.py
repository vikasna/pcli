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
read/mutate the same Session.messages list concurrently). `!`-prefixed
shell passthrough is the one exception - it never touches the Session at
all, so it runs immediately instead of queuing (see handle_text).

A fresh Session is started each time the daemon starts (same default `pcli
run` uses with no --session) - restarting `pcli telegram` begins a new
conversation. /new resets it without restarting the daemon.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from pcli.agent.headless import new_headless_session, run_headless_task
from pcli.agent.runtime import AgentRuntime
from pcli.config.settings import Settings, remove_config_keys, update_config_file
from pcli.cost.context import (
    ContextLimitTable,
    compute_max_response_tokens,
    set_model_context_limit,
)
from pcli.llm.errors import GatewayError
from pcli.permissions.guardrails import update_guardrails_limits
from pcli.permissions.manager import PermissionManager
from pcli.session.store import SessionStore
from pcli.telegram.permissions import (
    PendingApprovals,
    TelegramSender,
    ask_via_telegram,
    decode_callback_data,
)
from pcli.tui.shell_passthrough import run_passthrough_command

logger = logging.getLogger(__name__)


class _CommandError(Exception):
    """Raised by a scalar-setting command's `apply` closure (see
    TelegramDaemon._handle_scalar_setting_command) to report bad input -
    caught there and sent back as the reply, instead of propagating to
    run_forever's generic "something went wrong" handler. Not a GatewayError
    or anything network-related; purely a validation-failure message."""


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
        self._context_limit_table = ContextLimitTable.load()

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
        """Queues a message for processing - never runs it inline, except
        for `!`-prefixed shell passthrough (see handle_shell_passthrough),
        which bypasses the queue/session/LLM entirely, mirroring the TUI's
        own immediate, turn-independent handling of `!command` in
        ChatScreen.on_chat_input_submitted. See the module docstring for why
        queuing everything else matters."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        if text.startswith("!"):
            await self.handle_shell_passthrough(chat_id, text)
            return
        await self._queue.put(text)

    async def handle_shell_passthrough(self, chat_id: int, raw: str) -> None:
        """Mirrors ChatScreen's `!command`/`!!command` (tui/shell_passthrough.py)
        - runs the command directly against the real environment, bypassing
        the LLM, the sandbox, permissions, and session/artifact recording
        entirely. Safe to expose here under the same trust model as the TUI
        version: the chat-id authorization check above is this bot's
        equivalent of "the user's own keyboard" (see the module docstring's
        "personal automation, not a multi-user bot"). The TUI's `!!!`
        (handing off a real interactive terminal) has no Telegram
        equivalent - there's no TTY to hand off to - so it gets an
        explanatory reply instead of being attempted."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return

        if raw.startswith("!!!"):
            await self._sender.send_message(
                chat_id,
                "'!!!' (an interactive terminal) isn't supported over Telegram - there's "
                "no terminal to hand it off to. Use '!command' or '!!command' instead.",
            )
            return

        quiet = raw.startswith("!!")
        command = raw[2:].strip() if quiet else raw[1:].strip()
        if not command:
            await self._sender.send_message(
                chat_id,
                "Usage: !<command> to run a shell command (!!<command> to hide the result).",
            )
            return

        result = await run_passthrough_command(command, cwd=self._cwd)

        if quiet:
            await self._sender.send_message(chat_id, f"$ {command}\n(output hidden)")
            return

        output = result.stdout
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        footer = f"\n[exit_code={result.exit_code}]"
        if result.timed_out:
            footer += " (timed out)"
        await self._sender.send_message(chat_id, f"$ {command}\n{output}{footer}")

    async def _handle_scalar_setting_command(
        self,
        chat_id: int,
        arg: str | None,
        *,
        view: Callable[[], str],
        apply: Callable[[str], str],
    ) -> None:
        """Shared by every "/command [value]" setting below (mirrors the
        view-then-set shape of chat.py's equivalents, e.g.
        ChatScreen._handle_timeout_command): no value -> report the current
        one via `view`; a value -> `apply` it (parse, validate, persist,
        return the confirmation text - or raise _CommandError for bad
        input, same as a validation failure chat.py reports as a toast).
        Unlike chat.py, nothing here calls self._agent_loop.set_*() to
        apply a change immediately - TelegramDaemon has no persistent
        AgentLoop; run_headless_task builds a fresh one from self._settings
        on every message, so persisting the setting is already enough.
        Same authorization gate as handle_text."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        try:
            reply = view() if not arg else apply(arg)
        except _CommandError as exc:
            await self._sender.send_message(chat_id, str(exc))
            return
        await self._sender.send_message(chat_id, reply)

    async def handle_timeout_command(self, chat_id: int, arg: str | None) -> None:
        def view() -> str:
            current = self._settings.request_timeout_s
            effective = self._settings.effective_request_timeout_s
            note = (
                f" (effective: {effective:g}s - floored for local-api)"
                if effective != current
                else ""
            )
            return f"request_timeout_s is currently {current:g}s{note}. Usage: /timeout <seconds>"

        def apply(raw: str) -> str:
            try:
                seconds = float(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number of seconds.") from None
            if seconds <= 0:
                raise _CommandError("request_timeout_s must be greater than 0.")
            self._settings.request_timeout_s = seconds
            update_config_file(request_timeout_s=seconds)
            return f"request_timeout_s set to {seconds:g}s - takes effect on the next gateway request."

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_temperature_command(self, chat_id: int, arg: str | None) -> None:
        def view() -> str:
            current = self._settings.default_temperature
            text = f"{current:g}" if current is not None else "unset (gateway/model default)"
            return f"default_temperature is currently {text}. Usage: /temperature <value>|off"

        def apply(raw: str) -> str:
            if raw == "off":
                self._settings.default_temperature = None
                remove_config_keys("default_temperature")
                return "default_temperature cleared - gateway/model default applies."
            try:
                value = float(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number, or 'off'.") from None
            if value < 0:
                raise _CommandError("Temperature must be 0 or greater.")
            self._settings.default_temperature = value
            update_config_file(default_temperature=value)
            return f"default_temperature set to {value:g} - takes effect on the next turn."

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_budget_command(self, chat_id: int, arg: str | None) -> None:
        spent = self._session.cost.session_total_usd

        def view() -> str:
            current = self._settings.max_session_cost_usd
            text = f"${current:.2f}" if current is not None else "unset (no cap)"
            return (
                f"max_session_cost_usd is currently {text} (spent so far: ${spent:.4f}). "
                "Usage: /budget <amount>|off"
            )

        def apply(raw: str) -> str:
            if raw == "off":
                self._settings.max_session_cost_usd = None
                remove_config_keys("max_session_cost_usd")
                return "max_session_cost_usd cleared - no cap."
            try:
                value = float(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number, or 'off'.") from None
            if value <= 0:
                raise _CommandError(
                    "Budget must be greater than 0 (use /budget off to clear it)."
                )
            self._settings.max_session_cost_usd = value
            update_config_file(max_session_cost_usd=value)
            return (
                f"max_session_cost_usd set to ${value:.2f} (spent so far: ${spent:.4f}) - "
                "takes effect on the next round-trip."
            )

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_context_limit_command(self, chat_id: int, arg: str | None) -> None:
        model = self._session.model or self._settings.default_model

        def view() -> str:
            if not model:
                raise _CommandError("No model configured to set a context limit for.")
            current = self._context_limit_table.lookup(model)
            return (
                f"Assumed context limit for '{model}': {current:,} tokens. Usage: "
                "/context_limit <tokens>"
            )

        def apply(raw: str) -> str:
            if not model:
                raise _CommandError("No model configured to set a context limit for.")
            try:
                limit = int(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number of tokens.") from None
            if limit <= 0:
                raise _CommandError("Context limit must be greater than 0.")
            set_model_context_limit(model, limit)
            self._context_limit_table = ContextLimitTable.load()
            return f"Context limit for '{model}' set to {limit:,} tokens."

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_max_tool_iterations_command(self, chat_id: int, arg: str | None) -> None:
        def view() -> str:
            current = self._settings.max_tool_iterations
            note = " (currently uncapped: local-api mode)" if self._settings.is_local_api() else ""
            return f"max_tool_iterations is currently {current}{note}. Usage: /max_tool_iterations <n>"

        def apply(raw: str) -> str:
            try:
                iterations = int(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number of iterations.") from None
            if iterations <= 0:
                raise _CommandError("max_tool_iterations must be greater than 0.")
            self._settings.max_tool_iterations = iterations
            update_config_file(max_tool_iterations=iterations)
            note = (
                " This session is in local-api mode, so it stays uncapped until that changes."
                if self._settings.is_local_api()
                else " Takes effect on the next turn."
            )
            return f"max_tool_iterations set to {iterations}.{note}"

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_artifact_threshold_command(self, chat_id: int, arg: str | None) -> None:
        def view() -> str:
            current = self._settings.artifact_threshold_chars
            return (
                f"artifact_threshold_chars is currently {current:,}. Usage: "
                "/artifact_threshold <chars>"
            )

        def apply(raw: str) -> str:
            try:
                threshold = int(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number of characters.") from None
            if threshold <= 0:
                raise _CommandError("artifact_threshold_chars must be greater than 0.")
            self._settings.artifact_threshold_chars = threshold
            update_config_file(artifact_threshold_chars=threshold)
            return (
                f"artifact_threshold_chars set to {threshold:,}. Takes effect on the next "
                "tool result."
            )

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def _handle_guardrail_rate_limit_command(
        self,
        chat_id: int,
        arg: str | None,
        *,
        attr_name: str,
        config_key: str,
        command_name: str,
        window: str,
    ) -> None:
        """Shared by handle_max_tool_calls_per_turn_command and
        handle_max_tool_calls_per_minute_command - mirrors chat.py's own
        _handle_guardrail_rate_limit_command (both view/set the same-shaped
        guardrails.toml [limits] key, where 0 means unlimited)."""
        guardrails = self._permission_manager.guardrails

        def view() -> str:
            current = getattr(guardrails, attr_name)
            if self._settings.is_local_api():
                note = " (currently forced unlimited: local-api mode)"
            elif current <= 0:
                note = " (0 = unlimited)"
            else:
                note = ""
            return f"{config_key} is currently {current}{note}. Usage: /{command_name} <n> (0 = unlimited)"

        def apply(raw: str) -> str:
            try:
                value = int(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't a valid number.") from None
            if value < 0:
                raise _CommandError(f"{config_key} must be 0 or greater (0 means unlimited).")
            update_guardrails_limits(**{config_key: value})
            if self._settings.is_local_api():
                note = " This session is in local-api mode, so it stays unlimited until that changes."
            else:
                setattr(guardrails, attr_name, value)
                note = f" Takes effect on the next {window}."
            return f"{config_key} set to {value}.{note}"

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_max_tool_calls_per_turn_command(self, chat_id: int, arg: str | None) -> None:
        await self._handle_guardrail_rate_limit_command(
            chat_id,
            arg,
            attr_name="max_tool_calls_per_turn",
            config_key="max_tool_calls_per_turn",
            command_name="max_tool_calls_per_turn",
            window="turn",
        )

    async def handle_max_tool_calls_per_minute_command(self, chat_id: int, arg: str | None) -> None:
        await self._handle_guardrail_rate_limit_command(
            chat_id,
            arg,
            attr_name="max_tool_calls_per_minute",
            config_key="max_tool_calls_per_minute",
            command_name="max_tool_calls_per_minute",
            window="tool call",
        )

    async def handle_prune_tool_results_command(self, chat_id: int, arg: str | None) -> None:
        def view() -> str:
            state = "enabled" if self._settings.prune_tool_results_enabled else "disabled"
            return (
                f"prune_tool_results is {state}, keeping the most recent "
                f"{self._settings.prune_tool_results_keep_recent_turns} turn(s) verbatim. "
                "Usage: /prune_tool_results off|on|<n>"
            )

        def apply(raw: str) -> str:
            if raw == "off":
                self._settings.prune_tool_results_enabled = False
                update_config_file(prune_tool_results_enabled=False)
                return "prune_tool_results disabled."
            if raw == "on":
                self._settings.prune_tool_results_enabled = True
                update_config_file(prune_tool_results_enabled=True)
                return "prune_tool_results enabled."
            try:
                keep_recent_turns = int(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't 'off', 'on', or a valid number.") from None
            if keep_recent_turns <= 0:
                raise _CommandError(
                    "keep_recent_turns must be greater than 0 (use /prune_tool_results off "
                    "to disable pruning entirely)."
                )
            self._settings.prune_tool_results_enabled = True
            self._settings.prune_tool_results_keep_recent_turns = keep_recent_turns
            update_config_file(
                prune_tool_results_enabled=True,
                prune_tool_results_keep_recent_turns=keep_recent_turns,
            )
            return (
                f"prune_tool_results_keep_recent_turns set to {keep_recent_turns}. Takes "
                "effect on the next turn."
            )

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_max_response_tokens_command(self, chat_id: int, arg: str | None) -> None:
        def view() -> str:
            state = "enabled" if self._settings.max_response_tokens_enabled else "disabled"
            current = compute_max_response_tokens(
                self._session,
                limit_table=self._context_limit_table,
                safety_margin=self._settings.max_response_tokens_safety_margin,
            )
            current_text = (
                f"{current:,} tokens" if current is not None else "no cap (not yet computable)"
            )
            return (
                f"max_response_tokens is {state}, safety margin "
                f"{self._settings.max_response_tokens_safety_margin:,} tokens. Would "
                f"currently send max_tokens={current_text}. Usage: /max_response_tokens "
                "off|on|<margin>"
            )

        def apply(raw: str) -> str:
            if raw == "off":
                self._settings.max_response_tokens_enabled = False
                update_config_file(max_response_tokens_enabled=False)
                return "max_response_tokens disabled."
            if raw == "on":
                self._settings.max_response_tokens_enabled = True
                update_config_file(max_response_tokens_enabled=True)
                return "max_response_tokens enabled."
            try:
                safety_margin = int(raw)
            except ValueError:
                raise _CommandError(f"'{raw}' isn't 'off', 'on', or a valid number.") from None
            if safety_margin <= 0:
                raise _CommandError(
                    "safety margin must be greater than 0 (use /max_response_tokens off to "
                    "disable the cap entirely)."
                )
            self._settings.max_response_tokens_enabled = True
            self._settings.max_response_tokens_safety_margin = safety_margin
            update_config_file(
                max_response_tokens_enabled=True,
                max_response_tokens_safety_margin=safety_margin,
            )
            return (
                f"max_response_tokens_safety_margin set to {safety_margin:,}. Takes effect "
                "on the next turn."
            )

        await self._handle_scalar_setting_command(chat_id, arg, view=view, apply=apply)

    async def handle_new_command(self, chat_id: int) -> None:
        if not self._is_authorized(chat_id):
            return
        self._session = new_headless_session(self._store, self._settings, self._cwd)
        await self._sender.send_message(chat_id, "Started a new session.")

    async def handle_unsupported_command(self, chat_id: int, command: str) -> None:
        """Telegram doesn't have every TUI slash command yet (e.g. /models,
        /sessions, /plan aren't implemented here - see daemon.py's own
        handle_*_command methods for the current set). Without this,
        bot.py's handler registration means anything not explicitly
        registered matches no handler at all and is silently dropped by
        python-telegram-bot itself, before TelegramDaemon ever sees it - a
        real reported confusion ("I sent /models and nothing happened").
        Same authorization gate as handle_text/handle_new_command."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        await self._sender.send_message(
            chat_id,
            f"'{command}' isn't a command this Telegram bot supports yet. Anything else "
            "(no leading /) is sent to the agent as a normal message.",
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
        # interface.
        await self._sender.send_message(self._chat_id, "Working on it...")

        # run_headless_task's on_progress is a plain sync callback (it has
        # to be - it's called from inside an async-for loop it doesn't
        # control, see headless.py), so it can't await the real Telegram
        # send itself. It just queues the line; a background task drains
        # the queue and does the actual sending, so each tool call and its
        # result reaches the chat as it happens instead of only after the
        # whole turn finishes.
        progress: asyncio.Queue[str | None] = asyncio.Queue()

        def on_progress(line: str) -> None:
            if not line.startswith("> "):  # the echoed task text itself - redundant, skip
                progress.put_nowait(line)

        async def forward_progress() -> None:
            while True:
                line = await progress.get()
                if line is None:
                    return
                await self._sender.send_message(self._chat_id, line)

        forwarder = asyncio.create_task(forward_progress())

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
                on_progress=on_progress,
                ask=ask,
            )
        except GatewayError as exc:
            # Drained before sending the error, not after, so any tool
            # calls that did complete before the gateway failed still show
            # up in the chat ahead of the error message instead of behind it.
            await progress.put(None)
            await forwarder
            await self._sender.send_message(self._chat_id, f"Gateway error: {exc.message}")
            return

        await progress.put(None)
        await forwarder

        await self._sender.send_message(self._chat_id, result.final_text or "(no reply)")
        if result.truncations_exhausted:
            await self._sender.send_message(
                self._chat_id,
                "Response kept getting cut off by the token limit even after retrying - it "
                "may be incomplete. Send another message to continue.",
            )
