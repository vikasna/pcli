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
from dataclasses import replace
from pathlib import Path

from pcli.agent.compaction import maybe_compact
from pcli.agent.headless import new_headless_session, run_headless_task
from pcli.agent.runtime import AgentRuntime, make_tool_context
from pcli.config.settings import Settings, remove_config_keys, update_config_file
from pcli.cost.context import (
    ContextLimitTable,
    compute_max_response_tokens,
    current_context_usage,
    set_model_context_limit,
)
from pcli.cost.tracker import CostTracker
from pcli.llm.errors import GatewayError
from pcli.memory.extraction import extract_memory
from pcli.memory.models import render_memory_list
from pcli.memory.store import clear_memory, read_memory, remove_entry
from pcli.permissions.guardrails import update_guardrails_fs_allowed_roots, update_guardrails_limits
from pcli.permissions.manager import PermissionManager
from pcli.sandbox.base import SandboxSecurityError
from pcli.session.models import ToolInvocation
from pcli.session.store import SessionStore
from pcli.telegram.permissions import (
    PendingApprovals,
    TelegramSender,
    ask_via_telegram,
    decode_callback_data,
)
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.toolbox.manager import ToolboxDiscoveryError
from pcli.tui.shell_passthrough import run_passthrough_command

logger = logging.getLogger(__name__)

_HELP_TEXT = (
    "Commands:\n"
    "\n"
    "/help - show this help.\n"
    "/new - start a fresh session (does not restart the daemon).\n"
    "/rename [name] - view or set the current session's title.\n"
    "/timeout [seconds] - view or set the per-request gateway timeout.\n"
    "/temperature [value|off] - view or set the sampling temperature.\n"
    "/budget [amount|off] - view or set a hard cap on this session's spend.\n"
    "/context_limit [tokens] - view or set the assumed context window for the current model.\n"
    "/max_tool_iterations [n] - view or set the per-turn tool-call iteration cap.\n"
    "/artifact_threshold [chars] - view or set the tool-output length archived instead of "
    "kept inline.\n"
    "/max_tool_calls_per_turn [n] - view or set the per-turn tool-call guardrail (0 = "
    "unlimited).\n"
    "/max_tool_calls_per_minute [n] - view or set the per-minute tool-call guardrail (0 = "
    "unlimited).\n"
    "/prune_tool_results [off|on|n] - view, toggle, or set tool-result pruning.\n"
    "/max_response_tokens [off|on|margin] - view, toggle, or set the dynamic max_tokens cap.\n"
    "/allowed_roots [add|remove] [path] - view or edit the filesystem guardrail's allowed "
    "paths.\n"
    "/memory [forget <id>|clear] - view, trim, or clear pcli's cross-session memory of you.\n"
    "/toolbox [discover <name> [path]|list|remove <name>] - discover, list, or remove "
    "toolbox tools (CLI programs/scripts wrapped as callable tools).\n"
    "/compact - manually summarize the conversation so far to free up context space.\n"
    "/models [name] - set the model, or list what's available from the gateway.\n"
    "/sessions [switch <id>] - list other sessions, or switch to one.\n"
    "\n"
    "!<command> - run a shell command directly, bypassing the agent (!!<command> hides the "
    "output).\n"
)


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
        self._turn_in_progress = False

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

    async def handle_rename_command(self, chat_id: int, arg: str | None) -> None:
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        if not arg:
            await self._sender.send_message(
                chat_id,
                f"Current session title: '{self._session.derive_title()}'. Usage: /rename <name>",
            )
            return
        self._session.title = arg
        self._store.save(self._session)
        await self._sender.send_message(chat_id, f"Session renamed to '{arg}'.")

    async def handle_allowed_roots_command(self, chat_id: int, rest: str) -> None:
        """Mirrors ChatScreen._handle_allowed_roots_command - view or edit
        the filesystem guardrail's allowed_roots list. Telegram-side name
        is /allowed_roots, not /allowed-roots - Telegram bot commands can't
        contain hyphens (see the scalar-setting commands' own note on this).
        "add"/"remove" are plain arguments, not command names, so they keep
        their TUI spelling."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        guardrails = self._permission_manager.guardrails
        sub_command, _, arg = rest.partition(" ")
        sub_command = sub_command.strip().lower()
        arg = arg.strip()

        if not sub_command:
            roots = "\n".join(f"- {root}" for root in guardrails.fs_allowed_roots)
            await self._sender.send_message(
                chat_id,
                f"Current allowed_roots:\n{roots}\n\nUsage: /allowed_roots add <path> | "
                "/allowed_roots remove <path>",
            )
            return

        if sub_command == "add":
            if not arg:
                await self._sender.send_message(chat_id, "Usage: /allowed_roots add <path>")
                return
            if arg in guardrails.fs_allowed_roots:
                await self._sender.send_message(chat_id, f"'{arg}' is already in allowed_roots.")
                return
            new_roots = [*guardrails.fs_allowed_roots, arg]
            update_guardrails_fs_allowed_roots(new_roots)
            guardrails.fs_allowed_roots = new_roots
            await self._sender.send_message(
                chat_id, f"Added '{arg}' to allowed_roots. Takes effect immediately."
            )
            return

        if sub_command == "remove":
            if not arg:
                await self._sender.send_message(chat_id, "Usage: /allowed_roots remove <path>")
                return
            if arg not in guardrails.fs_allowed_roots:
                await self._sender.send_message(chat_id, f"'{arg}' isn't in allowed_roots.")
                return
            if len(guardrails.fs_allowed_roots) == 1:
                await self._sender.send_message(
                    chat_id,
                    "Refusing to remove the last allowed_roots entry - the agent needs at "
                    "least one, or every filesystem tool call would be denied.",
                )
                return
            new_roots = [root for root in guardrails.fs_allowed_roots if root != arg]
            update_guardrails_fs_allowed_roots(new_roots)
            guardrails.fs_allowed_roots = new_roots
            await self._sender.send_message(
                chat_id, f"Removed '{arg}' from allowed_roots. Takes effect immediately."
            )
            return

        await self._sender.send_message(
            chat_id,
            f"Unknown /allowed_roots subcommand: '{sub_command}'. Use /allowed_roots, "
            "/allowed_roots add <path>, or /allowed_roots remove <path>.",
        )

    async def handle_memory_command(self, chat_id: int, rest: str) -> None:
        """Mirrors ChatScreen._handle_memory_command - view, trim, or clear
        pcli's global, cross-session memory (memory/store.py)."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        sub_command, _, arg = rest.partition(" ")
        sub_command = sub_command.strip().lower()
        arg = arg.strip()

        if not sub_command:
            await self._sender.send_message(chat_id, render_memory_list(read_memory().entries))
            return

        if sub_command == "clear":
            clear_memory()
            await self._sender.send_message(chat_id, "Cleared all memory entries.")
            return

        if sub_command == "forget":
            if not arg:
                await self._sender.send_message(chat_id, "Usage: /memory forget <id>")
                return
            matches = [e for e in read_memory().entries if e.id.endswith(arg)]
            if not matches:
                await self._sender.send_message(chat_id, f"No memory entry found matching '{arg}'.")
                return
            if len(matches) > 1:
                await self._sender.send_message(
                    chat_id, f"'{arg}' matches more than one entry - use a longer id."
                )
                return
            remove_entry(matches[0].id)
            await self._sender.send_message(chat_id, f"Forgot: {matches[0].content}")
            return

        await self._sender.send_message(
            chat_id,
            f"Unknown /memory subcommand: '{sub_command}'. Use /memory, /memory forget <id>, "
            "or /memory clear.",
        )

    async def handle_help_command(self, chat_id: int) -> None:
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        await self._sender.send_message(chat_id, _HELP_TEXT)

    async def handle_toolbox_command(self, chat_id: int, rest: str) -> None:
        """Mirrors ChatScreen._handle_toolbox_command/_toolbox_discover/
        _toolbox_list/_toolbox_remove - discover, list, or remove toolbox
        tools (CLI programs/scripts wrapped as callable tools). Unlike
        chat.py, self._runtime.toolbox_manager and self._runtime.
        tool_registry are always present here (AgentRuntime builds both for
        every caller - see agent/runtime.py), so there's no "toolbox isn't
        available" guard to port."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        sub, _, arg = rest.partition(" ")
        arg = arg.strip()

        if sub == "discover" and arg:
            name, _, path = arg.partition(" ")
            await self._toolbox_discover(chat_id, name, path.strip() or None)
        elif sub == "list":
            await self._toolbox_list(chat_id)
        elif sub == "remove" and arg:
            await self._toolbox_remove(chat_id, arg)
        else:
            await self._sender.send_message(
                chat_id,
                "Usage: /toolbox discover <name> [path] | /toolbox list | "
                "/toolbox remove <name>",
            )

    async def _toolbox_discover(self, chat_id: int, name: str, path: str | None) -> None:
        await self._sender.send_message(chat_id, f"Discovering '{name}'...")
        try:
            summary = await self._runtime.toolbox_manager.discover(
                name,
                gateway_client=self._runtime.client,
                model=self._settings.default_model or None,
                path=path,
            )
        except (ToolboxDiscoveryError, GatewayError, SandboxSecurityError) as exc:
            await self._sender.send_message(chat_id, f"Discovery failed: {exc}")
            return
        await self._sender.send_message(chat_id, summary)

        loaded = await self._runtime.toolbox_manager.load_all()
        self._runtime.tool_registry.merge(loaded)

    async def _toolbox_list(self, chat_id: int) -> None:
        entries = self._runtime.toolbox_manager.list_discovered()
        if not entries:
            await self._sender.send_message(chat_id, "No software discovered yet. Try /toolbox discover <name>.")
            return
        lines = [
            f"- {name} [{entry['source']}] {entry.get('version', '?')} - "
            f"{entry.get('tool_count', 0)} tool(s)"
            for name, entry in entries.items()
        ]
        await self._sender.send_message(chat_id, "\n".join(lines))

    async def _toolbox_remove(self, chat_id: int, name: str) -> None:
        self._runtime.toolbox_manager.remove(name)
        await self._sender.send_message(chat_id, f"Removed '{name}' from the toolbox.")

    def _effective_compaction_model(self) -> str | None:
        """Mirrors ChatScreen._effective_compaction_model - settings.
        compaction_model if configured (a deliberately cheaper/smaller
        model for compaction/memory-extraction's mechanical, lower-stakes
        calls), falling back to default_model/session.model otherwise."""
        return (
            self._settings.compaction_model
            or self._settings.default_model
            or self._session.model
            or None
        )

    async def handle_compact_command(self, chat_id: int) -> None:
        """Mirrors ChatScreen._run_compaction's "manual" path - unlike
        run_headless_task (which, deliberately narrower than ChatScreen's
        own turn loop, never auto-compacts - see headless.py's module
        docstring), the Telegram daemon has no other way to free up
        context on a long-running conversation, so this is a real need
        here, not just parity for its own sake."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        if self._turn_in_progress:
            await self._sender.send_message(
                chat_id, "Still working on the current turn - try /compact again once it's done."
            )
            return

        artifact_store = SessionArtifactStore(self._store, self._session.id)
        cost_tracker = CostTracker(self._session)
        configured_keep_recent_turns = self._settings.auto_compact_keep_recent_turns
        tightened_to: int | None = None

        try:
            result = await maybe_compact(
                self._session,
                gateway_client=self._runtime.client,
                model=self._effective_compaction_model(),
                artifact_store=artifact_store,
                keep_recent_turns=configured_keep_recent_turns,
            )
            if result is None:
                usage = current_context_usage(self._session, limit_table=self._context_limit_table)
                if usage.fraction >= self._settings.auto_compact_threshold:
                    for smaller in range(configured_keep_recent_turns - 1, -1, -1):
                        result = await maybe_compact(
                            self._session,
                            gateway_client=self._runtime.client,
                            model=self._effective_compaction_model(),
                            artifact_store=artifact_store,
                            keep_recent_turns=smaller,
                        )
                        if result is not None:
                            tightened_to = smaller
                            break
        except GatewayError as exc:
            logger.exception("Gateway error during compaction: %s", exc.message)
            await self._sender.send_message(chat_id, f"Compaction failed: {exc.message}")
            return

        if result is None:
            await self._sender.send_message(chat_id, "Nothing to compact yet.")
            return

        self._session.tool_invocations.append(
            ToolInvocation(
                tool_name="_compaction",
                arguments={},
                status="ok",
                result_summary=f"Compacted {result.messages_compacted} message(s).",
                full_result_ref=SessionArtifactStore.blob_name_for(result.artifact_id),
            )
        )
        cost_tracker.record_turn(
            self._effective_compaction_model() or "", result.usage, source="compaction"
        )
        tightened_note = (
            f" (kept only the last {tightened_to} recent turn(s) verbatim instead of the "
            f"usual {configured_keep_recent_turns} - context was still full at that setting)"
            if tightened_to is not None
            else ""
        )
        await self._sender.send_message(
            chat_id,
            f"Compacted {result.messages_compacted} earlier message(s) to reduce context "
            f"usage (archived as artifact_id='{result.artifact_id}'){tightened_note}.",
        )
        self._store.save(self._session)

        if self._settings.memory_enabled:
            await self._extract_memory_from(result.artifact_id, artifact_store, cost_tracker)

    async def _extract_memory_from(
        self, artifact_id: str, artifact_store: SessionArtifactStore, cost_tracker: CostTracker
    ) -> None:
        """Mirrors ChatScreen._extract_memory_from - best-effort: a failure
        here is logged and otherwise invisible, since it never touched the
        turn/command that triggered compaction."""
        transcript = artifact_store.get(artifact_id)
        if not transcript:
            return
        model = self._effective_compaction_model()
        try:
            extraction_ctx = replace(
                make_tool_context(
                    self._runtime,
                    self._settings,
                    self._cwd,
                    session=self._session,
                    permission_manager=self._permission_manager,
                    artifact_store=artifact_store,
                ),
                model=model,
            )
            usages = await extract_memory(transcript, extraction_ctx)
        except GatewayError as exc:
            logger.exception("Gateway error during memory extraction: %s", exc.message)
            return
        if not usages:
            return
        for usage in usages:
            cost_tracker.record_turn(model or "", usage, source="memory")
        self._store.save(self._session)

    async def handle_models_command(self, chat_id: int, arg: str | None) -> None:
        """Mirrors ChatScreen._handle_models_command's set-directly path.
        The no-arg listing is plain text here, not chat.py's interactive
        ModelListScreen picker - see the plan's note on this; a future
        change may upgrade it to inline buttons the same way permission
        prompts already use them."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        if arg:
            self._set_model(arg)
            await self._sender.send_message(chat_id, f"Model set to '{arg}'.")
            return

        try:
            models = await self._runtime.client.list_models()
        except GatewayError as exc:
            await self._sender.send_message(chat_id, f"Failed to list models: {exc.message}")
            return

        if not models:
            await self._sender.send_message(chat_id, "Gateway returned no models.")
            return

        current = self._settings.default_model or self._session.model or None
        lines = [f"- {m}{' (active)' if m == current else ''}" for m in models]
        await self._sender.send_message(
            chat_id, "Available models:\n" + "\n".join(lines) + "\n\nUsage: /models <name>"
        )

    def _set_model(self, model: str) -> None:
        self._settings.default_model = model
        self._session.model = model
        update_config_file(default_model=model)

    async def handle_sessions_command(self, chat_id: int, rest: str) -> None:
        """`/sessions` lists other sessions (`SessionStore.list_index` -
        already sorted most-recently-updated first); `/sessions switch
        <id>` loads one and makes it self._session - the same
        single-attribute swap /new already does safely (an in-flight
        turn, if any, holds its own reference to the old Session object
        via run_headless_task's `session` parameter, so swapping
        self._session mid-turn doesn't corrupt anything in flight).
        Unlike `pcli --resume`, which requires the full id, `<id>` here
        can be any suffix of it - same convenience /memory forget <id>
        already gives, since typing a full id on a phone keyboard is
        impractical. Not chat.py's full interactive SessionListScreen
        browser (sort/search/delete) - a text list covers the real need
        (resuming a previous conversation from the phone)."""
        if not self._is_authorized(chat_id):
            logger.warning("Ignored message from unauthorized chat id %s", chat_id)
            return
        sub_command, _, arg = rest.partition(" ")
        sub_command = sub_command.strip().lower()
        arg = arg.strip()

        if not sub_command:
            entries = [e for e in self._store.list_index() if e.id != self._session.id][:20]
            if not entries:
                await self._sender.send_message(chat_id, "No other sessions yet.")
                return
            lines = [
                f"- {entry.id[-4:]}  {entry.title}  ({entry.message_count} msg, "
                f"${entry.total_cost_usd:.4f}, {entry.updated_at:%Y-%m-%d %H:%M})"
                for entry in entries
            ]
            await self._sender.send_message(
                chat_id,
                "Other sessions (most recent first):\n"
                + "\n".join(lines)
                + "\n\nUsage: /sessions switch <id> (the short id shown above, or more of it)",
            )
            return

        if sub_command == "switch":
            if not arg:
                await self._sender.send_message(chat_id, "Usage: /sessions switch <id>")
                return
            matches = [e for e in self._store.list_index() if e.id.endswith(arg)]
            if not matches:
                await self._sender.send_message(chat_id, f"No session found matching '{arg}'.")
                return
            if len(matches) > 1:
                await self._sender.send_message(
                    chat_id, f"'{arg}' matches more than one session - use a longer id."
                )
                return
            self._session = self._store.load(matches[0].id)
            await self._sender.send_message(
                chat_id, f"Switched to session '{self._session.derive_title()}'."
            )
            return

        await self._sender.send_message(
            chat_id,
            f"Unknown /sessions subcommand: '{sub_command}'. Use /sessions, or /sessions "
            "switch <id>.",
        )

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
        # Guards /compact below the same way ChatScreen._turn_in_progress
        # guards its own /compact - maybe_compact mutates session.messages
        # directly, which would race a turn also reading/appending to that
        # same list if the two ran concurrently.
        self._turn_in_progress = True
        try:
            await self._process_turn(text)
        finally:
            self._turn_in_progress = False

    async def _process_turn(self, text: str) -> None:
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
