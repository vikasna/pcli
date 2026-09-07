"""Main chat screen: streams a conversation against the configured gateway,
dispatching tool calls through permissions + sandbox, persisting to a Session
after every turn."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import Screen

from pcli.agent.activity import ActivityTracker
from pcli.agent.compaction import maybe_compact
from pcli.agent.context_pruning import extract_purpose, prune_old_tool_results
from pcli.agent.loop import AgentLoop, ToolResultEvent
from pcli.agent.prompt import build_system_prompt
from pcli.config.settings import Settings, get_settings, remove_config_keys, update_config_file
from pcli.cost.context import (
    ContextLimitTable,
    compute_max_response_tokens,
    current_context_usage,
    looks_like_context_ceiling,
    set_model_context_limit,
)
from pcli.cost.pricing_table import ModelPricing, PricingTable
from pcli.cost.tracker import CostTracker
from pcli.llm.client import GatewayClient
from pcli.llm.errors import GatewayError
from pcli.llm.models import ChatMessage, Usage
from pcli.permissions.guardrails import GuardrailsConfig, update_guardrails_limits
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.sandbox.base import Sandbox, SandboxSecurityError
from pcli.sandbox.selector import select_sandbox
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.session.export import export_session
from pcli.session.models import Message, Session, ToolInvocation
from pcli.session.store import SessionStore
from pcli.tools.agent_tools_store import load_persisted_agent_tools
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import AskQuestionCallback, ToolContext
from pcli.tools.registry import ToolRegistry, build_default_registry
from pcli.tools.toolbox.manager import ToolboxDiscoveryError, ToolboxManager
from pcli.tui.screens.ask_question_modal import ask_question_via_modal
from pcli.tui.screens.permission_modal import ask_via_modal
from pcli.tui.shell_passthrough import run_passthrough_command
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.message_view import MessageView
from pcli.tui.widgets.status_bar import StatusBar
from pcli.tui.widgets.status_pane import StatusPane

# Used for local-api-mode sessions: always $0, regardless of pricing.toml or
# the builtin table — a local model's name could otherwise coincidentally
# match a paid pattern there (e.g. "llama-3*") and show a fake nonzero cost.
_FREE_PRICING_TABLE = PricingTable(entries={}, default=ModelPricing())

# How long a second Escape press has to land after the first to count as a
# "confirm cancel" double-press (see ChatScreen.action_cancel_turn).
_ESCAPE_DOUBLE_PRESS_WINDOW_S = 0.6

# Ephemeral, per-turn reinforcement injected only while plan mode is active
# (see _run_one_turn) — never persisted to session.messages, so it can't be
# "forgotten" via compaction drift and never pollutes exports/resumption.
# The tool registry itself already blocks non-plan_mode_safe tools (and
# AgentLoop's dispatch-time backstop denies them even if one slipped through
# a stale registry) — this is a second, prompt-level layer on top of that,
# not the actual safety boundary.
_PLAN_MODE_REINFORCEMENT = (
    "# Plan mode active\n"
    "You are in plan mode: only read-only/exploration tools are available (writes, edits, "
    "shell commands, and other mutating actions will be denied if attempted). Investigate, "
    "explain your findings, and propose an approach — do not try to make changes or route "
    "around this restriction. The user will switch to /build before asking you to act on it."
)

# Static reference shown by /help — kept as one literal string (not built from
# the _handle_command dispatch table) since the wording needs full sentences,
# not just the bare command list already in ChatInput's placeholder text.
_HELP_TEXT = """\
# Commands

- **/help** — show this help.
- **/sessions** — browse, switch, or import previous sessions.
- **/export [path]** — export the current session (default: pcli's data \
directory).
- **/models [name]** — switch models. With no argument, lists models \
available from the configured gateway and lets you pick one.
- **/compact** — manually summarize the conversation so far to free up \
context space (also happens automatically as the context fills up).
- **/timeout [seconds]** — view or set the per-request timeout to the \
gateway.
- **/temperature [value|off]** — view or set the sampling temperature sent \
with each request; `off` clears it so no temperature field is sent at all \
(gateway/model default applies).
- **/context-limit [tokens]** — view or set the context window pcli assumes \
for the current model (used for the context-usage display and \
auto-compaction).
- **/max-tool-iterations [n]** — view or set how many tool-call round-trips \
a single turn can make before it's cut off (ignored — always uncapped — in \
local-api mode).
- **/artifact-threshold [chars]** — view or set the tool-output length \
beyond which results are archived out of the live conversation and \
retrieved later via fetch_artifact.
- **/max-tool-calls-per-turn [n]** — view or set the guardrail cap on tool \
calls within a single turn (0 = unlimited; ignored — always unlimited — in \
local-api mode).
- **/max-tool-calls-per-minute [n]** — view or set the guardrail cap on \
tool calls per minute, across the whole session (0 = unlimited; ignored \
— always unlimited — in local-api mode).
- **/prune-tool-results [off|on|n]** — view, toggle, or set how many \
recent turns' tool results stay verbatim before older ones are shrunk to \
a short placeholder (archived, retrievable via fetch_artifact) to save \
context — no LLM call involved, runs every turn.
- **/max-response-tokens [off|on|margin]** — view, toggle, or set the \
dynamic max_tokens cap sent with each request, leaving `margin` tokens of \
headroom below the model's context limit so a single response can't \
consume the entire remaining window by itself.
- **/rename [name]** — view or set the current session's title (shown in \
/sessions).
- **/plan** — enter plan mode: the agent can only use read-only/exploration \
tools (no writes, edits, or shell commands) until you exit.
- **/build** — exit plan mode, restoring full tool access.
- **/toolbox** — discover, list, or remove toolbox tools (CLI programs/\
scripts wrapped as callable tools): `/toolbox discover NAME [path]`, \
`/toolbox list`, `/toolbox remove NAME`.

# Shell passthrough

- **!command** — run a shell command directly (bypasses the model), showing \
its output.
- **!!command** — same, but hides the output.
- **!!!command** — hand off a real interactive terminal to the command \
(passwords, REPLs, editors, ssh — anything needing a real TTY).

# Input box

- **Enter** sends your message; **Ctrl+J** or **Alt+Enter** inserts a \
newline for a multi-line message. The box grows to fit what you type (up \
to 10 lines) and shrinks back down; a paste never grows it.
- **Up** / **Down** recall previously-sent messages, while the current \
draft has no newline in it.
"""

logger = logging.getLogger(__name__)


class ChatScreen(Screen):
    # No custom quit binding needed: Textual's own App already binds ctrl+q
    # to quit (with priority=True, so nothing here could override it even if
    # we wanted to), and ctrl+c is reserved by Textual itself as a
    # "press ctrl+q to quit" hint (App.action_help_quit) rather than quitting
    # directly — deliberately, to avoid killing the app on a reflexive
    # ctrl+c. A previous ("ctrl+c", "quit", "Quit") entry here never actually
    # fired (confirmed: Textual's system-level binding for the same key
    # always wins over a Screen-level one) and just misled anyone reading it.
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel_turn", "Cancel turn (press twice)"),
    ]

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings or get_settings()
        self._store = store or SessionStore()
        self._client: GatewayClient | None = None
        self._agent_loop: AgentLoop | None = None
        guardrails = GuardrailsConfig.load()
        if self._settings.is_local_api():
            # Local-api mode uncaps turn/rate limiting only — the security
            # guardrails (shell denylist, fs roots, module denylist) are
            # untouched.
            guardrails = guardrails.model_copy(
                update={"max_tool_calls_per_turn": 0, "max_tool_calls_per_minute": 0}
            )
        self._permission_manager = PermissionManager(guardrails=guardrails)
        self._sandbox: Sandbox | None = None
        self._tool_registry: ToolRegistry | None = None
        self._toolbox_manager: ToolboxManager | None = None
        self._current_ask: AskCallback | None = None
        self._current_ask_question: AskQuestionCallback | None = None
        self._cwd = Path.cwd()
        self._activity = ActivityTracker()
        # Lets a message submitted while a turn is already running be
        # queued and processed right after, instead of either being ignored
        # or (the bug this fixes) silently cancelling the in-flight turn —
        # see _stream_response/on_input_submitted.
        self._turn_in_progress = False
        self._has_queued_followup = False
        self._last_escape_at = 0.0
        self._plan_mode = False

        if session is not None:
            self._session = session
        else:
            self._session = self._store.new_session(
                model=self._settings.default_model,
                gateway_base_url=self._settings.gateway_base_url,
            )
            self._session.messages.append(
                Message(role="system", content=build_system_prompt())
            )

        pricing_table = _FREE_PRICING_TABLE if self._settings.is_local_api() else None
        self._cost_tracker = CostTracker(self._session, pricing_table=pricing_table)
        self._context_limit_table = ContextLimitTable.load()
        # Set when the startup probe in _maybe_detect_context_limit comes up
        # empty - worth one retry after the model has actually produced a
        # response, since a local gateway (LM Studio, in particular) only
        # reports a model's real loaded context size once it's actually
        # resident, and JIT-loads on first inference. Consumed (reset False)
        # by the one retry attempt in _run_one_turn, so a backend that
        # genuinely can't be probed (a hosted API) doesn't get re-hit every
        # turn for the rest of the session.
        self._context_limit_retry_pending = False
        self._artifact_store = SessionArtifactStore(self._store, self._session.id)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield StatusPane(id="status-pane")
            yield MessageView(id="message-view")
            yield StatusBar(id="status-bar")
            yield ChatInput(
                placeholder="Ask pcli... (/help for all commands — Enter to send, "
                "Ctrl+J for a newline)",
                id="input-box",
            )

    def _refresh_cost_display(self, status_bar: StatusBar) -> None:
        status_bar.session_cost_usd = self._session.cost.session_total_usd
        status_bar.total_tokens = self._session.cost.total_tokens

    def _refresh_context_display(self, status_bar: StatusBar, usage: Usage) -> None:
        # Deliberately driven by the parent loop's OWN usage events only (never
        # a subagent's, even though subagent usage also lands in
        # session.cost.turns for billing) — a subagent's isolated, much
        # smaller context isn't the conversation's actual context size.
        model = self._settings.default_model or self._session.model
        status_bar.context_used_tokens = usage.total_tokens
        status_bar.context_limit_tokens = self._context_limit_table.lookup(model)

    def _refresh_todo_pane(self) -> None:
        self.query_one(StatusPane).todos = list(self._session.todos)

    def _effective_max_tool_iterations(self) -> int | None:
        return None if self._settings.is_local_api() else self._settings.max_tool_iterations

    def _on_activity_changed(self) -> None:
        status_bar = self.query_one(StatusBar)
        sub = self._activity.subagent
        status_bar.subagent_task = sub.task if sub else None
        status_bar.subagent_tool_calls = sub.tool_calls if sub else 0
        status_bar.subagent_last_tool = sub.last_tool if sub else None

    async def on_mount(self) -> None:
        # Cheap housekeeping: clears out empty sessions left over from a
        # previous run (see SessionStore.prune_empty_sessions) so /sessions
        # doesn't accumulate clutter from every launch that never sent a
        # message. Excludes the just-started/resumed session itself, which
        # may still legitimately have zero messages at this exact point.
        self._store.prune_empty_sessions(exclude_session_ids=[self._session.id])
        self.query_one(ChatInput).focus()
        status_bar = self.query_one(StatusBar)
        status_bar.model = self._session.model or self._settings.default_model
        self._refresh_cost_display(status_bar)
        context_usage = current_context_usage(self._session, limit_table=self._context_limit_table)
        status_bar.context_used_tokens = context_usage.used_tokens
        status_bar.context_limit_tokens = context_usage.limit_tokens
        self._activity.subscribe(self._on_activity_changed)
        self._refresh_todo_pane()

        message_view = self.query_one(MessageView)
        for message in self._session.messages:
            if message.role in ("user", "assistant") and message.content:
                message_view.add_message(message.role, message.content)
            elif message.role == "tool" and message.content:
                message_view.add_message("tool", f"{message.name}: {message.content[:500]}")

        if self._session.todos:
            from pcli.tools.builtin.todo_tool import render_todos

            message_view.add_message(
                "system", f"Resuming with existing todos:\n{render_todos(self._session.todos)}"
            )

        if self._session.decisions:
            from pcli.tools.builtin.decision_tool import render_decisions

            message_view.add_message(
                "system",
                f"Resuming with {len(self._session.decisions)} recorded decision(s):\n"
                f"{render_decisions(self._session.decisions)}",
            )

        if not self._settings.is_configured():
            message_view.add_message(
                "system",
                "Gateway not configured. Set `PCLI_GATEWAY_URL` (and `PCLI_GATEWAY_API_KEY` if "
                "your gateway requires auth) or edit the config file, then restart pcli.",
            )
            return

        if self._settings.is_local_api():
            message_view.add_message(
                "system",
                "Local-API mode active for this gateway: max_tool_iterations and the "
                "guardrails' max_tool_calls_per_turn/per_minute are uncapped, and cost is "
                "forced to $0.",
            )

        try:
            self._sandbox = await select_sandbox(
                backend_override=self._settings.sandbox_backend, allowed_roots=[self._cwd]
            )
        except Exception as exc:  # noqa: BLE001 - surface sandbox setup failure, don't crash
            message_view.add_message("system", f"Sandbox setup failed: {exc}")
            return
        status_bar.sandbox_backend = self._sandbox.name

        self._tool_registry = build_default_registry()
        self._toolbox_manager = ToolboxManager(cwd=self._cwd)
        toolbox_tools = await self._toolbox_manager.load_all()
        self._tool_registry.merge(toolbox_tools)
        if len(toolbox_tools):
            message_view.add_message(
                "system", f"Loaded {len(toolbox_tools)} previously-discovered toolbox tool(s)."
            )

        agent_tools = load_persisted_agent_tools()
        self._tool_registry.merge(agent_tools)
        if len(agent_tools):
            message_view.add_message(
                "system", f"Loaded {len(agent_tools)} previously-registered agent tool(s)."
            )

        self._client = GatewayClient(self._settings)
        await self._maybe_detect_context_limit(message_view)
        self._agent_loop = AgentLoop(
            self._client,
            model=self._settings.default_model or None,
            tool_registry=self._tool_registry,
            permission_manager=self._permission_manager,
            tool_context_factory=self._make_tool_context,
            max_tool_iterations=self._effective_max_tool_iterations(),
            artifact_threshold_chars=self._settings.artifact_threshold_chars,
            temperature=self._settings.default_temperature,
        )

    async def _maybe_detect_context_limit(
        self, message_view: MessageView, *, is_retry: bool = False
    ) -> None:
        """Best-effort: if pcli has no context-window entry for this model
        yet, or the entry it has came from a previous auto-detect run
        (ContextLimitTable.should_attempt_detection — a manual
        /context-limit correction or a built-in default is never re-probed
        or overwritten, only a prior auto-detected value), ask the gateway
        directly (GatewayClient.detect_context_limit / cost/context_detect.py).
        Several backends (LM Studio, Ollama, a LiteLLM proxy, a raw
        llama.cpp server, and — for free, via the standard /models
        response — OpenRouter/vLLM) expose this; hosted-only gateways
        (OpenAI, Anthropic, ...) don't, in which case the notice below
        points at /context-limit instead. Re-probing an already-auto-detected
        model on every startup matters for local gateways specifically: LM
        Studio/Ollama let you reload the same model with a different context
        length, so a value pcli auto-detected once can silently go stale in
        a way a hosted API's fixed limit or a human's manual override never
        does — see a real debugged case in cost/context.py's
        looks_like_context_ceiling docstring. Wrapped defensively so a probe
        failure/timeout never blocks startup — this is a nice-to-have, not
        a requirement for the rest of on_mount to complete.

        `is_retry=True` is the one-shot retry from _run_one_turn's "usage"
        handling, after the model has actually produced a response - a
        gateway that JIT-loads (LM Studio) may not report a model's real
        loaded context size until then, even though should_attempt_detection
        said this model was worth probing. It only changes whether a second
        failure re-arms _context_limit_retry_pending: the initial call sets
        it so a retry gets scheduled, but the retry itself must not, or a
        backend that can never be probed (a hosted API) would get re-hit
        every single turn for the rest of the session instead of once."""
        if not self._settings.context_limit_auto_detect_enabled:
            return
        model = self._settings.default_model or self._session.model
        if not model or not self._context_limit_table.should_attempt_detection(model):
            return
        try:
            limit = await self._client.detect_context_limit(model)
        except Exception:  # noqa: BLE001 - never let a probe failure block startup
            limit = None

        if limit:
            set_model_context_limit(model, limit, auto_detected=True)
            self._context_limit_table = ContextLimitTable.load()
            self.query_one(StatusBar).context_limit_tokens = self._context_limit_table.lookup(model)
            message_view.add_message(
                "system", f"Auto-detected context limit for '{model}': {limit:,} tokens."
            )
        else:
            if not is_retry:
                self._context_limit_retry_pending = True
            assumed = self._context_limit_table.lookup(model)
            message_view.add_message(
                "system",
                f"Couldn't auto-detect a context limit for '{model}' — pcli is assuming "
                f"{assumed:,} tokens. If that's wrong, set it with /context-limit <tokens>.",
            )

    def _effective_tool_registry(self) -> ToolRegistry | None:
        """self._tool_registry filtered to plan_mode_safe tools while plan
        mode is active, recomputed fresh each time (rather than cached)
        since self._tool_registry itself can grow after startup, e.g. via
        register_toolbox_tool/register_agent_tool)."""
        if self._tool_registry is None:
            return None
        if self._plan_mode:
            return self._tool_registry.filtered(lambda t: t.plan_mode_safe)
        return self._tool_registry

    def _make_tool_context(self) -> ToolContext:
        assert self._sandbox is not None
        return ToolContext(
            sandbox=self._sandbox,
            guardrails=self._permission_manager.guardrails,
            cwd=self._cwd,
            gateway_client=self._client,
            model=self._settings.default_model or None,
            tool_registry=self._effective_tool_registry(),
            permission_manager=self._permission_manager,
            ask=self._current_ask,
            ask_question=self._current_ask_question,
            brave_search_api_key=self._settings.brave_search_api_key,
            max_tool_iterations=self._effective_max_tool_iterations(),
            session=self._session,
            artifact_store=self._artifact_store,
            activity=self._activity,
            toolbox_manager=self._toolbox_manager,
            plan_mode=self._plan_mode,
        )

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if isinstance(self._sandbox, RestrictedSubprocessSandbox):
            await self._sandbox.kill_all_background_jobs()

    def action_cancel_turn(self) -> None:
        """Esc+Esc: cancels the in-flight turn (the streaming reply and/or
        whatever tool is currently executing as part of it). A no-op with no
        turn running, so idly pressing Escape does nothing. Requires two
        presses within _ESCAPE_DOUBLE_PRESS_WINDOW_S so a single reflexive
        Escape (e.g. dismissing a thought, or a stray keypress) can't
        accidentally kill real work — the first press just shows a hint."""
        if not self._turn_in_progress:
            return

        now = time.monotonic()
        if now - self._last_escape_at <= _ESCAPE_DOUBLE_PRESS_WINDOW_S:
            self._last_escape_at = 0.0
            message_view = self.query_one(MessageView)
            # _stream_response's while-loop (see its docstring) is what
            # cancel_group actually kills. If a follow-up was queued right
            # before this, _has_queued_followup would stay stuck True with
            # no live loop left to consult it, and a LATER, unrelated turn's
            # loop-check would spuriously run an extra empty-input
            # _run_one_turn() — clearing it here is required, not optional.
            dropped_followup = self._has_queued_followup
            self._has_queued_followup = False
            self.workers.cancel_group(self, "agent-turn")
            note = "Turn cancelled."
            if dropped_followup:
                note += " A queued follow-up message was not sent."
            message_view.add_message("system", note)
        else:
            self._last_escape_at = now
            self.query_one(MessageView).add_message(
                "system", "Press Esc again to cancel the current turn."
            )

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        text = event.chat_input.consume_pending_paste(text)
        if not text:
            return
        event.chat_input.text = ""
        event.chat_input.add_to_history(text)

        if text.startswith("!!!"):
            self._run_interactive_shell(text[3:].strip())
            return

        if text.startswith("!"):
            self._run_shell_passthrough(text)
            return

        if text.startswith("/"):
            self._handle_command(text)
            return

        if self._agent_loop is None:
            return
        message_view = self.query_one(MessageView)
        self._session.messages.append(Message(role="user", content=text))
        message_view.add_message("user", text)
        if self._turn_in_progress:
            # Queue it rather than starting a second _stream_response worker
            # (which, on the same exclusive group, would cancel the one
            # already running instead of running alongside or after it) —
            # the message is already visible above; _stream_response picks
            # it up itself once the current turn finishes.
            self._has_queued_followup = True
        else:
            self._stream_response()

    def _run_interactive_shell(self, command: str) -> None:
        """Runs `!!!cmd` with a real terminal handed to it (passwords,
        REPLs, editors, ssh — anything that needs an actual TTY, which the
        pipe-based !/!! passthrough below can't provide since Textual
        already owns pcli's own stdin). Deliberately a plain sync method,
        not a @work worker: App.suspend() is itself a synchronous context
        manager (Textual stops reading/writing the terminal entirely for
        its duration), so there's nothing useful the event loop could do
        concurrently anyway — blocking here is the correct behavior, not a
        workaround."""
        message_view = self.query_one(MessageView)
        if not command:
            message_view.add_message(
                "system", "Usage: !!!<command> to run a command with a real interactive terminal"
            )
            return

        message_view.add_message("shell", f"→ Handing off terminal to: {command}")
        try:
            with self.app.suspend():
                exit_code = subprocess.run(
                    command, shell=True, cwd=str(self._cwd), check=False
                ).returncode
        except SuspendNotSupported:
            message_view.add_message(
                "system", "Interactive shell handoff isn't supported in this terminal environment."
            )
            return
        message_view.add_message(
            "shell", f"$ {command}  (ran interactively)\n[exit_code={exit_code}]"
        )

    @work(exclusive=False)
    async def _run_shell_passthrough(self, raw: str) -> None:
        """Runs a raw shell command the user typed directly (`!cmd`, or
        `!!cmd` to hide the result). Deliberately bypasses the LLM, the
        sandbox, permissions, and session/artifact recording entirely — this
        never touches self._session or gets sent to the model in any way."""
        message_view = self.query_one(MessageView)
        quiet = raw.startswith("!!")
        command = raw[2:].strip() if quiet else raw[1:].strip()
        if not command:
            message_view.add_message(
                "system", "Usage: !<command> to run a shell command (!!<command> to hide the result)"
            )
            return

        result = await run_passthrough_command(command, cwd=self._cwd)

        if quiet:
            message_view.add_message("shell", f"$ {command}\n(output hidden)")
            return

        output = result.stdout
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        footer = f"\n[exit_code={result.exit_code}]"
        if result.timed_out:
            footer += " (timed out)"
        message_view.add_message("shell", f"$ {command}\n{output}{footer}")

    def _handle_command(self, text: str) -> None:
        message_view = self.query_one(MessageView)
        command, _, rest = text[1:].partition(" ")
        rest = rest.strip()

        if command == "sessions":
            from pcli.tui.screens.sessions import SessionListScreen

            self.app.push_screen(
                SessionListScreen(self._store, exclude_session_id=self._session.id)
            )
        elif command == "export":
            self._export_current(rest or None)
        elif command == "toolbox":
            self._handle_toolbox_command(rest)
        elif command == "models":
            self._handle_models_command(rest or None)
        elif command == "compact":
            self._manual_compact()
        elif command == "timeout":
            self._handle_timeout_command(rest or None)
        elif command == "temperature":
            self._handle_temperature_command(rest or None)
        elif command == "context-limit":
            self._handle_context_limit_command(rest or None)
        elif command == "max-tool-iterations":
            self._handle_max_tool_iterations_command(rest or None)
        elif command == "artifact-threshold":
            self._handle_artifact_threshold_command(rest or None)
        elif command == "max-tool-calls-per-turn":
            self._handle_guardrail_rate_limit_command(
                rest or None,
                attr_name="max_tool_calls_per_turn",
                config_key="max_tool_calls_per_turn",
                command_name="max-tool-calls-per-turn",
                window="turn",
            )
        elif command == "max-tool-calls-per-minute":
            self._handle_guardrail_rate_limit_command(
                rest or None,
                attr_name="max_tool_calls_per_minute",
                config_key="max_tool_calls_per_minute",
                command_name="max-tool-calls-per-minute",
                window="tool call",
            )
        elif command == "prune-tool-results":
            self._handle_prune_tool_results_command(rest or None)
        elif command == "max-response-tokens":
            self._handle_max_response_tokens_command(rest or None)
        elif command == "rename":
            self._handle_rename_command(rest or None)
        elif command == "plan":
            self._set_plan_mode(True)
        elif command == "build":
            self._set_plan_mode(False)
        elif command == "help":
            self._handle_help_command()
        else:
            message_view.add_message("system", f"Unknown command: /{command}")

    def _handle_timeout_command(self, arg: str | None) -> None:
        """`/timeout [seconds]` — GatewayClient reads request_timeout_s fresh
        on every request (see llm/client.py's per-request timeout override),
        so changing it here takes effect on the very next gateway call, no
        restart needed. Persisted the same way /models persists a
        selection, so it's remembered next time too."""
        message_view = self.query_one(MessageView)
        if not arg:
            current = self._settings.request_timeout_s
            effective = self._settings.effective_request_timeout_s
            note = f" (effective: {effective:g}s — floored for local-api)" if effective != current else ""
            message_view.add_message(
                "system", f"request_timeout_s is currently {current:g}s{note}. Usage: /timeout <seconds>"
            )
            return

        try:
            seconds = float(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't a valid number of seconds.")
            return
        if seconds <= 0:
            message_view.add_message("system", "request_timeout_s must be greater than 0.")
            return

        self._settings.request_timeout_s = seconds
        update_config_file(request_timeout_s=seconds)
        message_view.add_message(
            "system", f"request_timeout_s set to {seconds:g}s — takes effect on the next gateway request."
        )

    def _handle_temperature_command(self, arg: str | None) -> None:
        """`/temperature [value|off]` — sets the sampling temperature sent
        with each request (AgentLoop.set_temperature -> GatewayClient.
        chat_stream's temperature param), taking effect on the very next
        turn. `off` clears it back to "unset" (no temperature field sent at
        all, so the gateway/model's own default applies) — this needs
        remove_config_keys, not update_config_file, since update_config_file
        deliberately skips writing a None value rather than persisting the
        removal."""
        message_view = self.query_one(MessageView)
        if not arg:
            current = self._settings.default_temperature
            text = f"{current:g}" if current is not None else "unset (gateway/model default)"
            message_view.add_message(
                "system", f"default_temperature is currently {text}. Usage: /temperature <value>|off"
            )
            return

        if arg == "off":
            self._settings.default_temperature = None
            remove_config_keys("default_temperature")
            if self._agent_loop is not None:
                self._agent_loop.set_temperature(None)
            message_view.add_message(
                "system", "default_temperature cleared — gateway/model default applies."
            )
            return

        try:
            value = float(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't a valid number, or 'off'.")
            return
        if value < 0:
            message_view.add_message("system", "Temperature must be 0 or greater.")
            return

        self._settings.default_temperature = value
        update_config_file(default_temperature=value)
        if self._agent_loop is not None:
            self._agent_loop.set_temperature(value)
        message_view.add_message(
            "system", f"default_temperature set to {value:g} — takes effect on the next turn."
        )

    def _handle_context_limit_command(self, arg: str | None) -> None:
        """`/context-limit [tokens]` — sets or shows the context-window size
        pcli assumes for the current model (ContextLimitTable, cost/context.py).
        Wrong by default for any model without a built-in or user-configured
        entry (silently falls back to a generic 128000-token guess), which
        disables auto-compaction for a model with a much smaller real
        window — see the context-ceiling notice in _run_one_turn, which
        points here. Persists to context_limits.toml and reloads the table
        immediately, so it takes effect without a restart."""
        message_view = self.query_one(MessageView)
        model = self._session.model or self._settings.default_model
        if not model:
            message_view.add_message("system", "No model configured to set a context limit for.")
            return

        if not arg:
            current = self._context_limit_table.lookup(model)
            message_view.add_message(
                "system",
                f"Assumed context limit for '{model}': {current:,} tokens. Usage: "
                "/context-limit <tokens>",
            )
            return

        try:
            limit = int(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't a valid number of tokens.")
            return
        if limit <= 0:
            message_view.add_message("system", "Context limit must be greater than 0.")
            return

        set_model_context_limit(model, limit)
        self._context_limit_table = ContextLimitTable.load()
        message_view.add_message("system", f"Context limit for '{model}' set to {limit:,} tokens.")

    def _handle_max_tool_iterations_command(self, arg: str | None) -> None:
        """`/max-tool-iterations [n]` — caps how many tool-call round-trips
        a single turn can make before it's cut off (see AgentLoop.run_turn's
        iteration guardrail). Ignored in local-api mode, which always runs
        uncapped (_effective_max_tool_iterations returns None there) —
        still saved for whenever local-api mode is off."""
        message_view = self.query_one(MessageView)
        if not arg:
            current = self._settings.max_tool_iterations
            note = " (currently uncapped: local-api mode)" if self._settings.is_local_api() else ""
            message_view.add_message(
                "system",
                f"max_tool_iterations is currently {current}{note}. Usage: "
                "/max-tool-iterations <n>",
            )
            return

        try:
            iterations = int(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't a valid number of iterations.")
            return
        if iterations <= 0:
            message_view.add_message("system", "max_tool_iterations must be greater than 0.")
            return

        self._settings.max_tool_iterations = iterations
        update_config_file(max_tool_iterations=iterations)
        if self._agent_loop is not None:
            self._agent_loop.set_max_tool_iterations(self._effective_max_tool_iterations())
        note = (
            " This session is in local-api mode, so it stays uncapped until that changes."
            if self._settings.is_local_api()
            else " Takes effect on the next turn."
        )
        message_view.add_message(
            "system", f"max_tool_iterations set to {iterations}.{note}"
        )

    def _handle_artifact_threshold_command(self, arg: str | None) -> None:
        """`/artifact-threshold [chars]` — tool results longer than this are
        truncated out of the live conversation and archived to the artifact
        library, retrievable via fetch_artifact (see AgentLoop._archive_if_large
        and Settings.artifact_threshold_chars). Persisted the same way
        /timeout persists request_timeout_s."""
        message_view = self.query_one(MessageView)
        if not arg:
            current = self._settings.artifact_threshold_chars
            message_view.add_message(
                "system",
                f"artifact_threshold_chars is currently {current:,}. Usage: "
                "/artifact-threshold <chars>",
            )
            return

        try:
            threshold = int(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't a valid number of characters.")
            return
        if threshold <= 0:
            message_view.add_message("system", "artifact_threshold_chars must be greater than 0.")
            return

        self._settings.artifact_threshold_chars = threshold
        update_config_file(artifact_threshold_chars=threshold)
        if self._agent_loop is not None:
            self._agent_loop.set_artifact_threshold_chars(threshold)
        message_view.add_message(
            "system",
            f"artifact_threshold_chars set to {threshold:,}. Takes effect on the next tool result.",
        )

    def _handle_guardrail_rate_limit_command(
        self, arg: str | None, *, attr_name: str, config_key: str, command_name: str, window: str
    ) -> None:
        """Shared by `/max-tool-calls-per-turn` and `/max-tool-calls-per-minute`
        — both view/set the same-shaped guardrails.toml [limits] key
        (`GuardrailsConfig`, `src/pcli/permissions/guardrails.py`), where 0
        means unlimited (see AgentLoop.run_turn's per-turn check and
        PermissionManager._within_rate_limit's per-minute check).

        In local-api mode, ChatScreen.__init__ already force-copies both of
        these to 0 (unlimited) for the live guardrails instance — a new
        value is still persisted here for whenever local-api mode is off,
        but isn't applied live, so the response says as much rather than
        implying it silently took effect."""
        message_view = self.query_one(MessageView)
        guardrails = self._permission_manager.guardrails
        if not arg:
            current = getattr(guardrails, attr_name)
            if self._settings.is_local_api():
                note = " (currently forced unlimited: local-api mode)"
            elif current <= 0:
                note = " (0 = unlimited)"
            else:
                note = ""
            message_view.add_message(
                "system",
                f"{config_key} is currently {current}{note}. Usage: /{command_name} <n> (0 = unlimited)",
            )
            return

        try:
            value = int(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't a valid number.")
            return
        if value < 0:
            message_view.add_message(
                "system", f"{config_key} must be 0 or greater (0 means unlimited)."
            )
            return

        update_guardrails_limits(**{config_key: value})
        if self._settings.is_local_api():
            note = (
                " This session is in local-api mode, so it stays unlimited until that changes."
            )
        else:
            setattr(guardrails, attr_name, value)
            note = f" Takes effect on the next {window}."
        message_view.add_message("system", f"{config_key} set to {value}.{note}")

    def _handle_prune_tool_results_command(self, arg: str | None) -> None:
        """`/prune-tool-results [off|on|<n>]` — view/toggle/set the
        no-LLM-call tool-result pruning pass (see
        agent/context_pruning.py's prune_old_tool_results, run from
        _run_one_turn). No-arg reports the current enabled state and
        keep_recent_turns. `off`/`on` toggles prune_tool_results_enabled.
        A positive integer sets prune_tool_results_keep_recent_turns and
        implicitly re-enables it. Persisted to config.toml the same way
        /timeout persists request_timeout_s; applied live since
        _run_one_turn reads these settings fresh every turn."""
        message_view = self.query_one(MessageView)
        if not arg:
            state = "enabled" if self._settings.prune_tool_results_enabled else "disabled"
            message_view.add_message(
                "system",
                f"prune_tool_results is {state}, keeping the most recent "
                f"{self._settings.prune_tool_results_keep_recent_turns} turn(s) verbatim. "
                "Usage: /prune-tool-results off|on|<n>",
            )
            return

        if arg == "off":
            self._settings.prune_tool_results_enabled = False
            update_config_file(prune_tool_results_enabled=False)
            message_view.add_message("system", "prune_tool_results disabled.")
            return

        if arg == "on":
            self._settings.prune_tool_results_enabled = True
            update_config_file(prune_tool_results_enabled=True)
            message_view.add_message("system", "prune_tool_results enabled.")
            return

        try:
            keep_recent_turns = int(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't 'off', 'on', or a valid number.")
            return
        if keep_recent_turns <= 0:
            message_view.add_message(
                "system",
                "keep_recent_turns must be greater than 0 (use /prune-tool-results off to "
                "disable pruning entirely).",
            )
            return

        self._settings.prune_tool_results_enabled = True
        self._settings.prune_tool_results_keep_recent_turns = keep_recent_turns
        update_config_file(
            prune_tool_results_enabled=True,
            prune_tool_results_keep_recent_turns=keep_recent_turns,
        )
        message_view.add_message(
            "system",
            f"prune_tool_results_keep_recent_turns set to {keep_recent_turns}. "
            "Takes effect on the next turn.",
        )

    def _handle_max_response_tokens_command(self, arg: str | None) -> None:
        """`/max-response-tokens [off|on|<n>]` — view/toggle/set the dynamic
        per-request max_tokens cap (compute_max_response_tokens,
        cost/context.py), computed fresh before every turn in
        _run_one_turn so a single response can't consume the entire
        remaining context window by itself — see that function's docstring
        for the real session this fixes. No-arg reports the enabled state,
        the configured safety margin, and what the cap would currently
        compute to. `off`/`on` toggles max_response_tokens_enabled. A
        positive integer sets max_response_tokens_safety_margin (tokens of
        headroom reserved below the model's context limit) and implicitly
        re-enables it. Persisted to config.toml, applied live."""
        message_view = self.query_one(MessageView)
        if not arg:
            state = "enabled" if self._settings.max_response_tokens_enabled else "disabled"
            current = compute_max_response_tokens(
                self._session,
                limit_table=self._context_limit_table,
                safety_margin=self._settings.max_response_tokens_safety_margin,
            )
            current_text = f"{current:,} tokens" if current is not None else "no cap (not yet computable)"
            message_view.add_message(
                "system",
                f"max_response_tokens is {state}, safety margin "
                f"{self._settings.max_response_tokens_safety_margin:,} tokens. Would currently "
                f"send max_tokens={current_text}. Usage: /max-response-tokens off|on|<margin>",
            )
            return

        if arg == "off":
            self._settings.max_response_tokens_enabled = False
            update_config_file(max_response_tokens_enabled=False)
            message_view.add_message("system", "max_response_tokens disabled.")
            return

        if arg == "on":
            self._settings.max_response_tokens_enabled = True
            update_config_file(max_response_tokens_enabled=True)
            message_view.add_message("system", "max_response_tokens enabled.")
            return

        try:
            safety_margin = int(arg)
        except ValueError:
            message_view.add_message("system", f"'{arg}' isn't 'off', 'on', or a valid number.")
            return
        if safety_margin <= 0:
            message_view.add_message(
                "system",
                "safety margin must be greater than 0 (use /max-response-tokens off to "
                "disable the cap entirely).",
            )
            return

        self._settings.max_response_tokens_enabled = True
        self._settings.max_response_tokens_safety_margin = safety_margin
        update_config_file(
            max_response_tokens_enabled=True,
            max_response_tokens_safety_margin=safety_margin,
        )
        message_view.add_message(
            "system",
            f"max_response_tokens_safety_margin set to {safety_margin:,}. Takes effect on the "
            "next turn.",
        )

    def _handle_rename_command(self, arg: str | None) -> None:
        """`/rename [name]` — sets Session.title, which derive_title() (used
        by the /sessions list) prefers over the auto-derived first-message
        snippet. No-arg shows the current title."""
        message_view = self.query_one(MessageView)
        if not arg:
            message_view.add_message(
                "system", f"Current session title: '{self._session.derive_title()}'. Usage: /rename <name>"
            )
            return

        self._session.title = arg
        self._store.save(self._session)
        message_view.add_message("system", f"Session renamed to '{arg}'.")

    def _set_plan_mode(self, enabled: bool) -> None:
        """`/plan` (enter) / `/build` (exit) — restricts the agent to
        plan_mode_safe tools (read/explore only, no writes/edits/shell). The
        registry swap is the primary mechanism (the model never even sees a
        disallowed tool); AgentLoop's dispatch-time backstop and the
        per-turn prompt reinforcement (_PLAN_MODE_REINFORCEMENT) are the
        additional layers on top, not the boundary itself."""
        message_view = self.query_one(MessageView)
        if enabled == self._plan_mode:
            message_view.add_message(
                "system", f"Already in {'plan' if enabled else 'build'} mode."
            )
            return

        self._plan_mode = enabled
        self.query_one(StatusBar).plan_mode = enabled
        if self._agent_loop is not None:
            self._agent_loop.set_tool_registry(self._effective_tool_registry())

        if enabled:
            message_view.add_message(
                "system",
                "Plan mode enabled: only read-only/exploration tools are available. "
                "Use /build to exit and allow writes/edits/shell commands again.",
            )
        else:
            message_view.add_message("system", "Build mode restored: all tools are available again.")

    def _handle_help_command(self) -> None:
        """`/help` — a static reference of every slash command and shell
        prefix, rendered as Markdown (message_view._render_message already
        runs every system message through rich.markdown.Markdown)."""
        message_view = self.query_one(MessageView)
        message_view.add_message("system", _HELP_TEXT)

    def _handle_toolbox_command(self, rest: str) -> None:
        message_view = self.query_one(MessageView)
        sub, _, arg = rest.partition(" ")
        arg = arg.strip()

        if sub == "discover" and arg:
            # /toolbox discover <name> [path] - a second token registers a
            # self-authored script directly, bypassing PATH lookup (same
            # path= parameter register_toolbox_tool gives the LLM itself).
            name, _, path = arg.partition(" ")
            self._toolbox_discover(name, path.strip() or None)
        elif sub == "list":
            self._toolbox_list()
        elif sub == "remove" and arg:
            self._toolbox_remove(arg)
        else:
            message_view.add_message(
                "system",
                "Usage: /toolbox discover <name> [path] | /toolbox list | /toolbox remove <name>",
            )

    @work(exclusive=True)
    async def _toolbox_discover(self, name: str, path: str | None = None) -> None:
        message_view = self.query_one(MessageView)
        if self._toolbox_manager is None:
            message_view.add_message("system", "Toolbox isn't available (gateway/sandbox not set up).")
            return
        message_view.add_message("system", f"Discovering '{name}'...")
        try:
            summary = await self._toolbox_manager.discover(
                name, gateway_client=self._client, model=self._settings.default_model or None, path=path
            )
        except (ToolboxDiscoveryError, GatewayError, SandboxSecurityError) as exc:
            # discover() calls the gateway to synthesize tool schemas when
            # there's no curated plugin (toolbox/manager.py) - that call can
            # raise GatewayError same as any other, and it previously wasn't
            # caught here at all, crashing this worker silently.
            message_view.add_message("system", f"Discovery failed: {exc}")
            return
        message_view.add_message("system", summary)

        if self._tool_registry is not None:
            loaded = await self._toolbox_manager.load_all()
            self._tool_registry.merge(loaded)

    @work(exclusive=True)
    async def _toolbox_list(self) -> None:
        message_view = self.query_one(MessageView)
        if self._toolbox_manager is None:
            message_view.add_message("system", "Toolbox isn't available (gateway/sandbox not set up).")
            return
        entries = self._toolbox_manager.list_discovered()
        if not entries:
            message_view.add_message("system", "No software discovered yet. Try /toolbox discover <name>.")
            return
        lines = [
            f"{name} [{entry['source']}] {entry.get('version', '?')} - {entry.get('tool_count', 0)} tool(s)"
            for name, entry in entries.items()
        ]
        message_view.add_message("system", "\n".join(lines))

    @work(exclusive=True)
    async def _toolbox_remove(self, name: str) -> None:
        message_view = self.query_one(MessageView)
        if self._toolbox_manager is None:
            message_view.add_message("system", "Toolbox isn't available (gateway/sandbox not set up).")
            return
        self._toolbox_manager.remove(name)
        message_view.add_message("system", f"Removed '{name}' from the toolbox.")

    @work(exclusive=True)
    async def _handle_models_command(self, arg: str | None) -> None:
        message_view = self.query_one(MessageView)

        if arg:
            self._set_model(arg)
            message_view.add_message("system", f"Model set to '{arg}'.")
            return

        if not self._settings.gateway_base_url:
            message_view.add_message(
                "system", "No gateway URL configured. Set PCLI_GATEWAY_URL and restart pcli."
            )
            return

        client = self._client
        owns_client = client is None
        if client is None:
            client = GatewayClient(self._settings)

        try:
            models = await client.list_models()
        except GatewayError as exc:
            message_view.add_message("system", f"Failed to list models: {exc.message}")
            return
        finally:
            if owns_client:
                await client.aclose()

        if not models:
            message_view.add_message("system", "Gateway returned no models.")
            return

        from pcli.tui.screens.models import ModelListScreen

        current = self._settings.default_model or self._session.model or None
        selected = await self.app.push_screen_wait(ModelListScreen(models, current))
        if selected:
            self._set_model(selected)
            message_view.add_message("system", f"Model set to '{selected}'.")

    def _set_model(self, model: str) -> None:
        self._settings.default_model = model
        self._session.model = model
        if self._agent_loop is not None:
            self._agent_loop.set_model(model)
        self.query_one(StatusBar).model = model
        # Remembered for next time so a bare `pcli` picks it up.
        update_config_file(default_model=model)

    @work(exclusive=True)
    async def _export_current(self, out_path_arg: str | None) -> None:
        message_view = self.query_one(MessageView)
        from pcli.config.paths import data_dir

        out_path = (
            Path(out_path_arg).expanduser()
            if out_path_arg
            else data_dir() / "exports" / f"{self._session.id}.pcli-session.json"
        )
        export_session(self._session, out_path, store=self._store)
        message_view.add_message("system", f"Exported session to {out_path}")

    def _record_tool_invocation(self, event: ToolResultEvent) -> None:
        try:
            arguments = json.loads(event.tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        invocation = ToolInvocation(
            tool_name=event.tool_call.function.name,
            arguments=arguments,
            status="error" if event.is_error else "ok",
            result_summary=event.output,
        )
        if event.artifact_id:
            # AgentLoop already archived the full output (event.output is the
            # truncated preview) — point the session record at that same blob
            # rather than storing it a second time under a different scheme.
            invocation.full_result_ref = SessionArtifactStore.blob_name_for(event.artifact_id)
        self._session.tool_invocations.append(invocation)

    def _show_decision_notice(self, event: ToolResultEvent) -> None:
        """record_decision results are shown as a distinct, always-visible
        message (not the generic collapsed-by-default tool Collapsible) —
        the whole point of the decision log is that it's immediately
        scannable, not tucked away."""
        try:
            arguments = json.loads(event.tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        decision = arguments.get("decision", "") if isinstance(arguments, dict) else ""
        rationale = arguments.get("rationale", "") if isinstance(arguments, dict) else ""
        message_view = self.query_one(MessageView)
        message_view.add_message("decision", f"**{decision}**\n\n{rationale}")

    async def _run_compaction(self, reason: str) -> None:
        """Summarizes and archives the oldest turns of session.messages (see
        agent/compaction.py) — either after an "auto" trigger from
        _stream_response, or a "manual" /compact command. Plain async method
        (not @work) so _stream_response can just await it directly while
        already inside its own worker; /compact reaches it via the small
        @work-wrapped _manual_compact below, same pattern as every other
        synchronously-dispatched command in this file."""
        message_view = self.query_one(MessageView)
        status_bar = self.query_one(StatusBar)
        if self._client is None or self._agent_loop is None:
            if reason == "manual":
                message_view.add_message(
                    "system", "Compaction isn't available (gateway/sandbox not set up)."
                )
            return

        if reason == "manual" and self._turn_in_progress:
            # maybe_compact slices/replaces session.messages directly —
            # running it concurrently with an active turn (which also reads
            # and appends to that same list) could corrupt the conversation.
            # The "auto" trigger is never at risk of this: it only ever runs
            # sequentially, awaited from inside _run_one_turn itself, after
            # that turn has already finished.
            message_view.add_message(
                "system", "Still working on the current turn — try /compact again once it's done."
            )
            return

        status_bar.busy = True
        try:
            result = await maybe_compact(
                self._session,
                gateway_client=self._client,
                model=self._settings.default_model or self._session.model or None,
                artifact_store=self._artifact_store,
                keep_recent_turns=self._settings.auto_compact_keep_recent_turns,
            )
        except GatewayError as exc:
            # Previously uncaught here: maybe_compact's one summarization
            # call failing (e.g. a timeout) would crash straight out of this
            # worker with nothing shown to the user - the turn that
            # triggered auto-compaction had already completed and saved
            # successfully by this point, so this is a notice, not a lost
            # turn, but it still needs to be visible (context usage just
            # silently won't have shrunk).
            logger.exception("Gateway error during compaction: %s", exc.message)
            message_view.add_message("system", f"Compaction failed: {exc.message}")
            return
        finally:
            status_bar.busy = False

        if result is None:
            if reason == "manual":
                message_view.add_message("system", "Nothing to compact yet.")
            return

        # Reuses ToolInvocation.full_result_ref purely so export_session
        # (which only bundles blobs it finds referenced there) carries this
        # artifact along too — no real tool call happened.
        self._session.tool_invocations.append(
            ToolInvocation(
                tool_name="_compaction",
                arguments={},
                status="ok",
                result_summary=f"Compacted {result.messages_compacted} message(s).",
                full_result_ref=SessionArtifactStore.blob_name_for(result.artifact_id),
            )
        )
        # Real spend, so it counts toward cost — but deliberately not fed into
        # _refresh_context_display, for the same reason subagent usage isn't:
        # it's not the main conversation's context size. The status bar
        # self-corrects on the next real turn's usage report.
        self._cost_tracker.record_turn(
            self._settings.default_model or self._session.model, result.usage
        )
        self._refresh_cost_display(status_bar)
        message_view.add_message(
            "system",
            f"Compacted {result.messages_compacted} earlier message(s) to reduce context "
            f"usage (archived as artifact_id='{result.artifact_id}').",
        )
        self._store.save(self._session)

    @work(exclusive=True)
    async def _manual_compact(self) -> None:
        await self._run_compaction("manual")

    @work(exclusive=True, group="agent-turn")
    async def _stream_response(self) -> None:
        """Runs turns back-to-back for as long as new user input gets
        queued while the previous one was still in flight (see
        on_input_submitted) — matches how opencode/Claude Code let you
        queue follow-up messages instead of either blocking input or
        cancelling the in-flight turn.

        Given its own dedicated worker group so no other exclusive worker
        on this screen (compaction, /export, /toolbox, ...) can ever cancel
        it: they used to all share Textual's default group, and Textual
        cancels every other worker in a group whenever a new exclusive one
        in that same group starts — so submitting a second chat message (or
        any other command) while a turn was running silently killed it
        instead of queueing or running after."""
        self._turn_in_progress = True
        try:
            while True:
                await self._run_one_turn()
                if not self._has_queued_followup:
                    break
                self._has_queued_followup = False
        finally:
            self._turn_in_progress = False

    async def _run_one_turn(self) -> None:
        assert self._agent_loop is not None
        message_view = self.query_one(MessageView)
        status_bar = self.query_one(StatusBar)
        message_view.add_message("assistant", "")

        async def ask(tool_name: str, arguments: dict, risk_description: str):
            return await ask_via_modal(self.app, tool_name, arguments, risk_description)

        async def ask_question(question: str, options: list[str] | None) -> str:
            return await ask_question_via_modal(self.app, question, options)

        self._current_ask = ask
        self._current_ask_question = ask_question
        status_bar.busy = True

        # Reasoning models stream their chain-of-thought under a channel
        # separate from the actual reply (see llm/streaming.py) — buffered
        # here and flushed as a collapsed "Thinking" panel whenever real
        # content or a tool call follows, rather than shown live: a model
        # can spend its *entire* response reasoning without ever producing
        # visible text or a tool call, which previously looked exactly like
        # pcli doing nothing at all. had_any_content tracks that case so a
        # clear notice can be shown instead of a silently empty turn.
        reasoning_parts: list[str] = []
        had_any_content = False
        had_any_reasoning = False

        def flush_reasoning() -> None:
            nonlocal had_any_reasoning
            if reasoning_parts:
                had_any_reasoning = True
                message_view.add_reasoning("".join(reasoning_parts))
                reasoning_parts.clear()

        if self._settings.max_response_tokens_enabled:
            self._agent_loop.set_max_response_tokens(
                compute_max_response_tokens(
                    self._session,
                    limit_table=self._context_limit_table,
                    safety_margin=self._settings.max_response_tokens_safety_margin,
                )
            )
        else:
            self._agent_loop.set_max_response_tokens(None)

        try:
            chat_messages = [m.to_chat_message() for m in self._session.messages]
            if self._plan_mode:
                chat_messages.append(ChatMessage(role="system", content=_PLAN_MODE_REINFORCEMENT))
            async for chunk in self._agent_loop.run_turn(chat_messages, ask=ask):
                if chunk.kind == "text_delta":
                    had_any_content = True
                    flush_reasoning()
                    message_view.append_to_last(chunk.text)
                elif chunk.kind == "reasoning_delta":
                    reasoning_parts.append(chunk.text)
                elif chunk.kind == "usage":
                    self._cost_tracker.record_turn(
                        self._settings.default_model or self._session.model, chunk.usage
                    )
                    self._refresh_cost_display(status_bar)
                    self._refresh_context_display(status_bar, chunk.usage)
                    if self._context_limit_retry_pending:
                        # The model just produced a real response, so a
                        # local gateway that JIT-loads (LM Studio) must have
                        # it loaded now, even if it didn't at startup - one
                        # retry, not a per-turn habit (see the flag's own
                        # comment in __init__).
                        self._context_limit_retry_pending = False
                        await self._maybe_detect_context_limit(message_view, is_retry=True)
                elif chunk.kind == "tool_start":
                    had_any_content = True
                    flush_reasoning()
                    message_view.finish_streaming()
                    purpose = extract_purpose(chunk.tool_call.function.arguments)
                    message_view.add_tool_call(
                        chunk.tool_call.function.name,
                        chunk.tool_call.function.arguments,
                        purpose=purpose,
                    )
                    message_view.finish_streaming()
                elif chunk.kind == "tool_result":
                    self._record_tool_invocation(chunk)
                    if chunk.tool_call.function.name == "write_todos":
                        self._refresh_todo_pane()
                    for extra in chunk.extra_usage:
                        self._cost_tracker.record_turn(
                            self._settings.default_model or self._session.model, extra
                        )
                    if chunk.extra_usage:
                        self._refresh_cost_display(status_bar)
                    if chunk.tool_call.function.name == "record_decision" and not chunk.is_error:
                        self._show_decision_notice(chunk)
                    else:
                        message_view.add_tool_result(
                            chunk.tool_call.function.name, chunk.output, is_error=chunk.is_error
                        )
                    message_view.finish_streaming()
                elif chunk.kind == "turn_complete":
                    self._session.messages.extend(
                        Message.from_chat_message(m) for m in chunk.new_messages
                    )
        except GatewayError as exc:
            # Previously the only trace of this was a message that vanished
            # the moment the TUI closed — nothing was logged, and nothing
            # was saved, so a session that failed on its very first turn
            # left literally no record anywhere (not even the user's own
            # message). Log it (with traceback) and save whatever's already
            # in self._session.messages (system prompt, prior successful
            # turns, and the user message that triggered this attempt) so a
            # failed turn is actually debuggable afterward.
            logger.exception("Gateway error during turn: %s", exc.message)
            flush_reasoning()
            message_view.finish_streaming()
            message_view.add_message("system", f"Gateway error: {exc.message}")
            self._store.save(self._session)
            return
        finally:
            status_bar.busy = False
        flush_reasoning()
        message_view.finish_streaming()
        if not had_any_content:
            # Not an error - the model responded, just with nothing visible
            # (e.g. it used its whole response budget on reasoning above and
            # never got to an actual answer or tool call). Previously this
            # looked identical to pcli having silently failed to do anything.
            suffix = ' (see "Thinking" above)' if had_any_reasoning else ""
            note = f"The model didn't produce a reply or tool call this turn{suffix}."
            if looks_like_context_ceiling(self._session):
                # A real debugged case: pcli's assumed context limit for this
                # model was wrong (silently falling back to a generic 128k
                # guess), so the fraction-based auto-compact trigger never
                # fired even though the session had actually exhausted the
                # model's real, much smaller window. total_tokens plateauing
                # near its highest-ever value for this session, turn after
                # turn, is the tell — see looks_like_context_ceiling.
                assumed = self._context_limit_table.lookup(self._session.model)
                note += (
                    f" This looks like it may have hit the model's real context limit — pcli "
                    f"is currently assuming {assumed:,} tokens for '{self._session.model}', "
                    "which may be wrong. Try /context-limit <tokens> to correct it (so "
                    "auto-compaction can kick in), or /compact to free up space now."
                )
            else:
                note += " Try again, or ask something more focused."
            message_view.add_message("system", note)
        self._store.save(self._session)

        if self._settings.prune_tool_results_enabled:
            # Cheap, mechanical, no LLM call - runs before the auto-compact
            # check so compaction's own (LLM-cost) summarization has less
            # bulk to work with by the time its threshold is ever reached.
            pruned_count = prune_old_tool_results(
                self._session,
                keep_recent_turns=self._settings.prune_tool_results_keep_recent_turns,
                artifact_store=self._artifact_store,
            )
            if pruned_count:
                self._store.save(self._session)

        if self._settings.auto_compact_enabled:
            usage = current_context_usage(self._session, limit_table=self._context_limit_table)
            if usage.fraction >= self._settings.auto_compact_threshold:
                await self._run_compaction("auto")
