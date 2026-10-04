"""Main chat screen: streams a conversation against the configured gateway,
dispatching tool calls through permissions + sandbox, persisting to a Session
after every turn."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import Screen

from pcli.agent.activity import ActivityTracker, format_subagent_activity
from pcli.agent.compaction import maybe_compact
from pcli.agent.context_pruning import extract_purpose, prune_old_tool_results
from pcli.agent.loop import AgentLoop, ToolResultEvent
from pcli.agent.prompt import PLAN_MODE_REINFORCEMENT, build_system_prompt
from pcli.agent.runtime import (
    build_agent_runtime,
    build_permission_manager,
    effective_max_tool_iterations,
    record_tool_invocation,
)
from pcli.browser.session import BrowserSession
from pcli.config.settings import Settings, get_settings, remove_config_keys, update_config_file
from pcli.cost.context import (
    ContextLimitTable,
    compute_max_response_tokens,
    current_context_usage,
    looks_like_context_ceiling,
    set_model_context_limit,
)
from pcli.cost.pricing_table import ModelPricing, PricingTable
from pcli.cost.tracker import CostTracker, cost_budget_reason
from pcli.llm.client import GatewayClient
from pcli.llm.errors import GatewayError
from pcli.llm.models import ChatMessage, ToolCall, Usage
from pcli.memory.extraction import extract_memory
from pcli.memory.models import render_memory_list, render_memory_section
from pcli.memory.store import clear_memory, read_memory, remove_entry
from pcli.permissions.guardrails import update_guardrails_fs_allowed_roots, update_guardrails_limits
from pcli.permissions.manager import AskCallback
from pcli.sandbox.base import Sandbox, SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.session.export import export_session
from pcli.session.models import Message, Session, ToolInvocation
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import AskQuestionCallback, ToolContext
from pcli.tools.registry import ToolRegistry
from pcli.tools.toolbox.manager import ToolboxDiscoveryError, ToolboxManager
from pcli.tui.screens.ask_question_modal import ask_question_via_modal
from pcli.tui.screens.permission_modal import ask_via_modal
from pcli.tui.screens.subagent_activity_modal import SubagentActivityModal
from pcli.tui.shell_passthrough import run_passthrough_command
from pcli.tui.widgets.chat_input import ChatInput
from pcli.tui.widgets.command_suggestions import CommandSuggestions
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

# Shared between /subagent and the Ctrl+G live-activity panel (action_
# show_subagent_activity) - both need to say the same thing when there's
# nothing to show.
_NO_SUBAGENT_RUNNING_MESSAGE = "No subagent is currently running."

# A real debugged case: the model's response got cut off by the token limit
# mid-task (e.g. "Let me implement X:" with no tool call following, because
# there was no room left to make one) - previously indistinguishable from a
# deliberate, complete stop, so the turn just silently ended with genuinely
# unfinished work and the user had to notice and type "continue" themselves.
# _run_one_turn now does that automatically (see TurnCompleteEvent.
# response_truncated), capped at this many consecutive attempts so a model
# that's stuck hitting the limit every single time doesn't loop forever.
_MAX_CONSECUTIVE_AUTO_CONTINUES = 3
_AUTO_CONTINUE_MESSAGE = "Continue."

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
- **/budget [amount|off]** — view or set a hard cap on this session's total \
spend; the agent loop stops itself (with a clear notice) once spend reaches \
it, rather than only reporting cost after the fact. `off` clears it (no cap).
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
- **/allowed-roots [add|remove] [path]** — view or edit the filesystem \
guardrail's allowed_roots list (default: just the working directory). A \
path outside every allowed_roots entry is denied outright, before any \
permission prompt — this is how to widen what read_file/write_file/\
edit_file/list_dir/etc. can reach, not a prompt you can approve your way \
past.
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
- **/theme [name]** — view the available Textual themes (built-in and \
pcli's own vim-* ones) and which is active, or switch live and remember \
it for next time.
- **/plan** — enter plan mode: the agent can only use read-only/exploration \
tools (no writes, edits, or shell commands) until you exit.
- **/build** — exit plan mode, restoring full tool access.
- **/toolbox** — discover, list, or remove toolbox tools (CLI programs/\
scripts wrapped as callable tools): `/toolbox discover NAME [path]`, \
`/toolbox list`, `/toolbox remove NAME`.
- **/subagent** — show the currently-running subagent's task and its full \
tool-call history so far, plus any question it's currently waiting on an \
answer to. Nothing to show once it finishes (only its final text and \
tool-call count come back to the main conversation).
- **/memory [forget <id>|clear]** — view pcli's global, cross-session \
memory of you (nature of work, preferences, conversation style, common \
asks — injected into every session's system prompt), remove one entry, or \
clear it all. Grows automatically from what you explicitly ask to be \
remembered and, when a session gets long enough to auto-compact, from \
what pcli notices worth keeping.

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
- Typing **/** shows matching slash commands with a one-line description; \
**Up** / **Down** moves the highlight, **Tab** or **Enter** accepts it \
(without sending), and **Escape** dismisses the list.
"""

# Drives ChatInput's slash-command autocomplete (tui/widgets/command_
# suggestions.py) - a separate, terser list from _HELP_TEXT above (one-liners
# for a dropdown vs. full sentences for /help), so update both when adding,
# removing, or renaming a command. Order here is purely the order suggestions
# are listed in when several match (e.g. typing bare "/"), not otherwise
# significant - kept roughly matching _HELP_TEXT's own order for ease of
# cross-checking the two lists against each other.
_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("help", "Show the full command reference."),
    ("sessions", "Browse, switch, or import previous sessions."),
    ("export", "Export the current session."),
    ("models", "Switch models, or list what's available."),
    ("compact", "Summarize the conversation so far to free up context."),
    ("timeout", "View or set the per-request gateway timeout."),
    ("temperature", "View or set the sampling temperature."),
    ("budget", "View or set a hard cap on this session's total spend."),
    ("context-limit", "View or set the assumed context window for this model."),
    ("max-tool-iterations", "View or set the per-turn tool-call iteration cap."),
    ("artifact-threshold", "View or set the tool-output archiving threshold."),
    ("max-tool-calls-per-turn", "View or set the guardrail cap on tool calls per turn."),
    ("max-tool-calls-per-minute", "View or set the guardrail cap on tool calls per minute."),
    ("allowed-roots", "View or edit which filesystem paths the agent can reach."),
    ("prune-tool-results", "View, toggle, or set old tool-result pruning."),
    ("max-response-tokens", "View, toggle, or set the dynamic response-length cap."),
    ("rename", "View or set the current session's title."),
    ("theme", "View or switch the TUI's color theme."),
    ("plan", "Enter plan mode (read-only tools only)."),
    ("build", "Exit plan mode, restoring full tool access."),
    ("toolbox", "Discover, list, or remove toolbox tools."),
    ("subagent", "Show the currently-running subagent's task and history."),
    ("memory", "View, forget, or clear pcli's memory of you."),
]

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
        ("ctrl+g", "show_subagent_activity", "Subagent activity"),
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
        self._permission_manager = build_permission_manager(self._settings)
        self._sandbox: Sandbox | None = None
        self._tool_registry: ToolRegistry | None = None
        self._toolbox_manager: ToolboxManager | None = None
        self._browser_session: BrowserSession | None = None
        self._current_ask: AskCallback | None = None
        self._current_ask_question: AskQuestionCallback | None = None
        self._cwd = Path.cwd()
        self._activity = ActivityTracker()
        # Lets a message submitted while a turn is already running be
        # queued and processed right after, instead of either being ignored
        # or (the bug this fixes) silently cancelling the in-flight turn —
        # see _stream_response/on_input_submitted.
        self._turn_in_progress = False
        # Raw text of user messages submitted while _turn_in_progress - kept
        # out of session.messages (and, in the UI, shown via
        # MessageView.add_queued_user_message rather than add_message) until
        # the active turn actually finishes, so they can't land ahead of
        # that turn's own assistant reply in the history, or hijack
        # MessageView's in-flight streaming target - see on_chat_input_
        # submitted/_stream_response for where this is populated/flushed.
        self._queued_followups: list[str] = []
        # Unrelated to the queue above - set only by the auto-continue-
        # after-truncation path at the very end of a turn that's already
        # finished (see _run_one_turn's response_truncated handling), purely
        # to make _stream_response's loop run one more time.
        self._has_queued_followup = False
        self._last_escape_at = 0.0
        self._plan_mode = False
        self._consecutive_truncations = 0
        """How many turns in a row ended because the response was cut off by
        the token limit (TurnCompleteEvent.response_truncated), not a
        deliberate stop - see _run_one_turn's own handling. Reset whenever a
        turn completes without truncation, or the user sends a real message
        of their own (see on_chat_input_submitted)."""

        if session is not None:
            self._session = session
        else:
            self._session = self._store.new_session(
                model=self._settings.default_model,
                gateway_base_url=self._settings.gateway_base_url,
                working_dir=str(self._cwd),
            )
            extra_sections = []
            if self._settings.memory_enabled:
                memory_section = render_memory_section(read_memory().entries)
                if memory_section:
                    extra_sections.append(memory_section)
            self._session.messages.append(
                Message(
                    role="system",
                    content=build_system_prompt(extra_sections=extra_sections or None),
                )
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
            yield CommandSuggestions(id="command-suggestions")
            yield ChatInput(
                placeholder="Ask pcli... (/help for all commands — Enter to send, "
                "Ctrl+J for a newline)",
                id="input-box",
                commands=_SLASH_COMMANDS,
            )

    def on_chat_input_suggestions_changed(self, event: ChatInput.SuggestionsChanged) -> None:
        self.query_one(CommandSuggestions).update_suggestions(event.matches, event.index)

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
        return effective_max_tool_iterations(self._settings)

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

        self._replay_message_history()

        message_view = self.query_one(MessageView)
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
            runtime = await build_agent_runtime(self._settings, self._cwd)
        except Exception as exc:  # noqa: BLE001 - surface startup failure, don't crash
            message_view.add_message("system", f"Startup failed: {exc}")
            return
        self._sandbox = runtime.sandbox
        status_bar.sandbox_backend = self._sandbox.name
        self._tool_registry = runtime.tool_registry
        self._toolbox_manager = runtime.toolbox_manager
        self._browser_session = runtime.browser_session
        if runtime.toolbox_tools_loaded:
            message_view.add_message(
                "system",
                f"Loaded {runtime.toolbox_tools_loaded} previously-discovered toolbox tool(s).",
            )
        if runtime.agent_tools_loaded:
            message_view.add_message(
                "system",
                f"Loaded {runtime.agent_tools_loaded} previously-registered agent tool(s).",
            )

        self._client = runtime.client
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
            memory_enabled=self._settings.memory_enabled,
            memory_max_entries=self._settings.memory_max_entries,
            max_tool_iterations=self._effective_max_tool_iterations(),
            subagent_max_iterations=self._settings.subagent_max_iterations,
            max_response_tokens=self._agent_loop.max_response_tokens if self._agent_loop else None,
            temperature=self._agent_loop.temperature if self._agent_loop else None,
            session=self._session,
            max_session_cost_usd=self._settings.max_session_cost_usd,
            artifact_store=self._artifact_store,
            activity=self._activity,
            toolbox_manager=self._toolbox_manager,
            plan_mode=self._plan_mode,
            browser_session=self._browser_session,
        )

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if isinstance(self._sandbox, RestrictedSubprocessSandbox):
            await self._sandbox.kill_all_background_jobs()
        if self._browser_session is not None:
            await self._browser_session.close()

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
            dropped_count = len(self._queued_followups)
            self._queued_followups = []
            self._has_queued_followup = False
            self.workers.cancel_group(self, "agent-turn")
            note = "Turn cancelled."
            if dropped_count:
                plural = "s" if dropped_count != 1 else ""
                note += f" {dropped_count} queued follow-up message{plural} not sent."
            message_view.add_message("system", note)
        else:
            self._last_escape_at = now
            # A toast, not a transcript message - purely instructional, no
            # value in a permanent scrollback record (unlike "Turn
            # cancelled." above, a real conversational event).
            self.notify("Press Esc again to cancel the current turn.")

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
        self._consecutive_truncations = 0  # a real message means a fresh direction
        if self._turn_in_progress:
            # Queue it rather than starting a second _stream_response worker
            # (which, on the same exclusive group, would cancel the one
            # already running instead of running alongside or after it).
            # Deliberately NOT appended to session.messages and NOT shown via
            # add_message here - either would happen out of order relative
            # to the in-flight turn's own assistant reply (session.messages
            # would see this new user message before that reply, since
            # turn_complete hasn't fired yet) and add_message would hijack
            # MessageView's in-flight streaming target mid-response. Shown
            # via the non-disruptive add_queued_user_message instead;
            # _stream_response folds it into session.messages once the
            # active turn actually finishes.
            self._queued_followups.append(text)
            message_view.add_queued_user_message(text)
        else:
            self._session.messages.append(Message(role="user", content=text))
            message_view.add_message("user", text)
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
            # A toast, not a transcript message - a usage hint, same "glance
            # and maybe retype" reasoning as /timeout above.
            self.notify(
                "Usage: !!!<command> to run a command with a real interactive terminal",
                severity="warning",
            )
            return

        message_view.add_message("shell", f"→ Handing off terminal to: {command}")
        try:
            with self.app.suspend():
                exit_code = subprocess.run(
                    command, shell=True, cwd=str(self._cwd), check=False
                ).returncode
        except SuspendNotSupported:
            self.notify(
                "Interactive shell handoff isn't supported in this terminal environment.",
                severity="warning",
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
            # A toast, not a transcript message - same "glance and maybe
            # retype" reasoning as /timeout above.
            self.notify(
                "Usage: !<command> to run a shell command (!!<command> to hide the result)",
                severity="warning",
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
        elif command == "budget":
            self._handle_budget_command(rest or None)
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
        elif command == "allowed-roots":
            self._handle_allowed_roots_command(rest)
        elif command == "prune-tool-results":
            self._handle_prune_tool_results_command(rest or None)
        elif command == "subagent":
            self._handle_subagent_command()
        elif command == "memory":
            self._handle_memory_command(rest)
        elif command == "max-response-tokens":
            self._handle_max_response_tokens_command(rest or None)
        elif command == "rename":
            self._handle_rename_command(rest or None)
        elif command == "theme":
            self._handle_theme_command(rest or None)
        elif command == "plan":
            self._set_plan_mode(True)
        elif command == "build":
            self._set_plan_mode(False)
        elif command == "help":
            self._handle_help_command()
        else:
            # A toast, not a transcript message - a mistyped command isn't
            # worth a permanent scrollback record.
            self.notify(f"Unknown command: /{command}", severity="warning")

    def _handle_timeout_command(self, arg: str | None) -> None:
        """`/timeout [seconds]` — GatewayClient reads request_timeout_s fresh
        on every request (see llm/client.py's per-request timeout override),
        so changing it here takes effect on the very next gateway call, no
        restart needed. Persisted the same way /models persists a
        selection, so it's remembered next time too. A toast, not a
        transcript message - see the "Split system notices" design note:
        this is exactly the "glance and maybe retype" class of confirmation,
        trivially re-checked by running /timeout again with no argument."""
        if not arg:
            current = self._settings.request_timeout_s
            effective = self._settings.effective_request_timeout_s
            note = f" (effective: {effective:g}s — floored for local-api)" if effective != current else ""
            self.notify(f"request_timeout_s is currently {current:g}s{note}. Usage: /timeout <seconds>")
            return

        try:
            seconds = float(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number of seconds.", severity="warning")
            return
        if seconds <= 0:
            self.notify("request_timeout_s must be greater than 0.", severity="warning")
            return

        self._settings.request_timeout_s = seconds
        update_config_file(request_timeout_s=seconds)
        self.notify(f"request_timeout_s set to {seconds:g}s — takes effect on the next gateway request.")

    def _handle_temperature_command(self, arg: str | None) -> None:
        """`/temperature [value|off]` — sets the sampling temperature sent
        with each request (AgentLoop.set_temperature -> GatewayClient.
        chat_stream's temperature param), taking effect on the very next
        turn. `off` clears it back to "unset" (no temperature field sent at
        all, so the gateway/model's own default applies) — this needs
        remove_config_keys, not update_config_file, since update_config_file
        deliberately skips writing a None value rather than persisting the
        removal. A toast, not a transcript message - same "glance and
        maybe retype" reasoning as /timeout above."""
        if not arg:
            current = self._settings.default_temperature
            text = f"{current:g}" if current is not None else "unset (gateway/model default)"
            self.notify(f"default_temperature is currently {text}. Usage: /temperature <value>|off")
            return

        if arg == "off":
            self._settings.default_temperature = None
            remove_config_keys("default_temperature")
            if self._agent_loop is not None:
                self._agent_loop.set_temperature(None)
            self.notify("default_temperature cleared — gateway/model default applies.")
            return

        try:
            value = float(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number, or 'off'.", severity="warning")
            return
        if value < 0:
            self.notify("Temperature must be 0 or greater.", severity="warning")
            return

        self._settings.default_temperature = value
        update_config_file(default_temperature=value)
        if self._agent_loop is not None:
            self._agent_loop.set_temperature(value)
        self.notify(f"default_temperature set to {value:g} — takes effect on the next turn.")

    def _handle_budget_command(self, arg: str | None) -> None:
        """`/budget [amount|off]` — views or sets Settings.max_session_cost_usd,
        a hard cap on this session's total spend (agent/loop.py's
        AgentLoop.run_turn consults it, via cost/tracker.py's
        cost_budget_reason, at the top of every internal LLM round-trip - see
        that function's own docstring). Same persistence shape as /temperature:
        `off` needs remove_config_keys, not update_config_file, since the
        latter deliberately skips writing a None value rather than persisting
        a removal. Takes effect on the very next round-trip, no restart - the
        budget_check closure reads self._settings fresh each time it's
        called. A toast, not a transcript message - same "glance and maybe
        retype" reasoning as /timeout above."""
        spent = self._session.cost.session_total_usd
        if not arg:
            current = self._settings.max_session_cost_usd
            text = f"${current:.2f}" if current is not None else "unset (no cap)"
            self.notify(
                f"max_session_cost_usd is currently {text} (spent so far: ${spent:.4f}). "
                "Usage: /budget <amount>|off"
            )
            return

        if arg == "off":
            self._settings.max_session_cost_usd = None
            remove_config_keys("max_session_cost_usd")
            self.notify("max_session_cost_usd cleared — no cap.")
            return

        try:
            value = float(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number, or 'off'.", severity="warning")
            return
        if value <= 0:
            self.notify(
                "Budget must be greater than 0 (use /budget off to clear it).", severity="warning"
            )
            return

        self._settings.max_session_cost_usd = value
        update_config_file(max_session_cost_usd=value)
        self.notify(
            f"max_session_cost_usd set to ${value:.2f} (spent so far: ${spent:.4f}) — takes "
            "effect on the next round-trip."
        )

    def _handle_context_limit_command(self, arg: str | None) -> None:
        """`/context-limit [tokens]` — sets or shows the context-window size
        pcli assumes for the current model (ContextLimitTable, cost/context.py).
        Wrong by default for any model without a built-in or user-configured
        entry (silently falls back to a generic 128000-token guess), which
        disables auto-compaction for a model with a much smaller real
        window — see the context-ceiling notice in _run_one_turn, which
        points here. Persists to context_limits.toml and reloads the table
        immediately, so it takes effect without a restart. A toast, not a
        transcript message - same "glance and maybe retype" reasoning as
        /timeout above."""
        model = self._session.model or self._settings.default_model
        if not model:
            self.notify("No model configured to set a context limit for.", severity="warning")
            return

        if not arg:
            current = self._context_limit_table.lookup(model)
            self.notify(
                f"Assumed context limit for '{model}': {current:,} tokens. Usage: "
                "/context-limit <tokens>"
            )
            return

        try:
            limit = int(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number of tokens.", severity="warning")
            return
        if limit <= 0:
            self.notify("Context limit must be greater than 0.", severity="warning")
            return

        set_model_context_limit(model, limit)
        self._context_limit_table = ContextLimitTable.load()
        self.notify(f"Context limit for '{model}' set to {limit:,} tokens.")

    def _handle_max_tool_iterations_command(self, arg: str | None) -> None:
        """`/max-tool-iterations [n]` — caps how many tool-call round-trips
        a single turn can make before it's cut off (see AgentLoop.run_turn's
        iteration guardrail). Ignored in local-api mode, which always runs
        uncapped (_effective_max_tool_iterations returns None there) —
        still saved for whenever local-api mode is off. A toast, not a
        transcript message - same "glance and maybe retype" reasoning as
        /timeout above."""
        if not arg:
            current = self._settings.max_tool_iterations
            note = " (currently uncapped: local-api mode)" if self._settings.is_local_api() else ""
            self.notify(
                f"max_tool_iterations is currently {current}{note}. Usage: "
                "/max-tool-iterations <n>"
            )
            return

        try:
            iterations = int(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number of iterations.", severity="warning")
            return
        if iterations <= 0:
            self.notify("max_tool_iterations must be greater than 0.", severity="warning")
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
        self.notify(f"max_tool_iterations set to {iterations}.{note}")

    def _handle_artifact_threshold_command(self, arg: str | None) -> None:
        """`/artifact-threshold [chars]` — tool results longer than this are
        truncated out of the live conversation and archived to the artifact
        library, retrievable via fetch_artifact (see AgentLoop._archive_if_large
        and Settings.artifact_threshold_chars). Persisted the same way
        /timeout persists request_timeout_s. A toast, not a transcript
        message - same "glance and maybe retype" reasoning as /timeout
        above."""
        if not arg:
            current = self._settings.artifact_threshold_chars
            self.notify(
                f"artifact_threshold_chars is currently {current:,}. Usage: "
                "/artifact-threshold <chars>"
            )
            return

        try:
            threshold = int(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number of characters.", severity="warning")
            return
        if threshold <= 0:
            self.notify("artifact_threshold_chars must be greater than 0.", severity="warning")
            return

        self._settings.artifact_threshold_chars = threshold
        update_config_file(artifact_threshold_chars=threshold)
        if self._agent_loop is not None:
            self._agent_loop.set_artifact_threshold_chars(threshold)
        self.notify(f"artifact_threshold_chars set to {threshold:,}. Takes effect on the next tool result.")

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
        implying it silently took effect. A toast, not a transcript
        message - same "glance and maybe retype" reasoning as /timeout
        above."""
        guardrails = self._permission_manager.guardrails
        if not arg:
            current = getattr(guardrails, attr_name)
            if self._settings.is_local_api():
                note = " (currently forced unlimited: local-api mode)"
            elif current <= 0:
                note = " (0 = unlimited)"
            else:
                note = ""
            self.notify(
                f"{config_key} is currently {current}{note}. Usage: /{command_name} <n> (0 = unlimited)"
            )
            return

        try:
            value = int(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't a valid number.", severity="warning")
            return
        if value < 0:
            self.notify(f"{config_key} must be 0 or greater (0 means unlimited).", severity="warning")
            return

        update_guardrails_limits(**{config_key: value})
        if self._settings.is_local_api():
            note = (
                " This session is in local-api mode, so it stays unlimited until that changes."
            )
        else:
            setattr(guardrails, attr_name, value)
            note = f" Takes effect on the next {window}."
        self.notify(f"{config_key} set to {value}.{note}")

    def _handle_allowed_roots_command(self, rest: str) -> None:
        """`/allowed-roots [add|remove] <path>` — view or live-edit
        guardrails.toml's [fs] allowed_roots list (permissions/
        guardrails.py's evaluate_path, gating read_file/write_file/
        edit_file/list_dir/glob_search/grep/diff_files/apply_patch/
        download_file). A path outside every allowed_roots entry is a hard
        guardrail deny — checked before, and never overridable by, a
        permission prompt (see the system prompt's "Respecting
        guardrails") — this command is the actual, intended way to widen
        what the agent can reach, not something to route around via a
        prompt. Mirrors _handle_guardrail_rate_limit_command's persist-
        then-hot-reload shape, but for a list-valued [fs] key instead of a
        scalar [limits] one. A toast, not a transcript message - same
        "glance and maybe retype" reasoning as /timeout above."""
        guardrails = self._permission_manager.guardrails
        sub_command, _, arg = rest.partition(" ")
        sub_command = sub_command.strip().lower()
        arg = arg.strip()

        if not sub_command:
            roots = "\n".join(f"- {root}" for root in guardrails.fs_allowed_roots)
            self.notify(
                f"Current allowed_roots:\n{roots}\n\nUsage: /allowed-roots add <path> | "
                "/allowed-roots remove <path>"
            )
            return

        if sub_command == "add":
            if not arg:
                self.notify("Usage: /allowed-roots add <path>", severity="warning")
                return
            if arg in guardrails.fs_allowed_roots:
                self.notify(f"'{arg}' is already in allowed_roots.", severity="warning")
                return
            new_roots = [*guardrails.fs_allowed_roots, arg]
            update_guardrails_fs_allowed_roots(new_roots)
            guardrails.fs_allowed_roots = new_roots
            self.notify(f"Added '{arg}' to allowed_roots. Takes effect immediately.")
            return

        if sub_command == "remove":
            if not arg:
                self.notify("Usage: /allowed-roots remove <path>", severity="warning")
                return
            if arg not in guardrails.fs_allowed_roots:
                self.notify(f"'{arg}' isn't in allowed_roots.", severity="warning")
                return
            if len(guardrails.fs_allowed_roots) == 1:
                self.notify(
                    "Refusing to remove the last allowed_roots entry — the agent needs at "
                    "least one, or every filesystem tool call would be denied.",
                    severity="warning",
                )
                return
            new_roots = [root for root in guardrails.fs_allowed_roots if root != arg]
            update_guardrails_fs_allowed_roots(new_roots)
            guardrails.fs_allowed_roots = new_roots
            self.notify(f"Removed '{arg}' from allowed_roots. Takes effect immediately.")
            return

        self.notify(
            f"Unknown /allowed-roots subcommand: '{sub_command}'. Use /allowed-roots, "
            "/allowed-roots add <path>, or /allowed-roots remove <path>.",
            severity="warning",
        )

    def _handle_subagent_command(self) -> None:
        """`/subagent` — a one-off snapshot of the currently-running
        subagent's task and its full tool-call history so far (name +
        arguments), plus any question it's currently blocked on, logged as a
        system message in the chat transcript. The status bar's second line
        only has room for a one-line summary (last tool name); Ctrl+G
        (action_show_subagent_activity) opens the same detail as a
        live-updating panel instead of a static snapshot, for anyone
        actively watching rather than checking in once. The "nothing
        running" case is a toast - trivially re-checked by rerunning the
        command; the activity dump itself stays in the transcript since
        it's genuine reference content worth scrolling back to."""
        sub = self._activity.subagent
        if sub is None:
            self.notify(_NO_SUBAGENT_RUNNING_MESSAGE)
            return
        self.query_one(MessageView).add_message("system", format_subagent_activity(sub))

    def _handle_memory_command(self, rest: str) -> None:
        """`/memory` — lists pcli's global, cross-session memory of this
        user (nature of work, preferences, conversation style, common asks -
        see memory/models.py); `/memory forget <id>` removes one entry by
        its short id (last 4 chars, as shown in the listing, same
        abbreviation SessionListScreen uses for session ids); `/memory
        clear` wipes it. This is the transparency/control surface for
        memory/extraction.py's autonomous derivation - what got remembered
        should always be visible and correctable, never a silent background
        process. The no-arg listing stays in the transcript (reference
        content worth scrolling back to, same as /toolbox list); every
        other branch here is a toast - a quick confirmation or error,
        trivially re-checked by rerunning /memory."""
        sub_command, _, arg = rest.partition(" ")
        sub_command = sub_command.strip().lower()
        arg = arg.strip()

        if not sub_command:
            self.query_one(MessageView).add_message(
                "system", render_memory_list(read_memory().entries)
            )
            return

        if sub_command == "clear":
            clear_memory()
            self.notify("Cleared all memory entries.")
            return

        if sub_command == "forget":
            if not arg:
                self.notify("Usage: /memory forget <id>", severity="warning")
                return
            matches = [e for e in read_memory().entries if e.id.endswith(arg)]
            if not matches:
                self.notify(f"No memory entry found matching '{arg}'.", severity="warning")
                return
            if len(matches) > 1:
                self.notify(
                    f"'{arg}' matches more than one entry — use a longer id.",
                    severity="warning",
                )
                return
            remove_entry(matches[0].id)
            self.notify(f"Forgot: {matches[0].content}")
            return

        self.notify(
            f"Unknown /memory subcommand: '{sub_command}'. Use /memory, /memory forget "
            "<id>, or /memory clear.",
            severity="warning",
        )

    def action_show_subagent_activity(self) -> None:
        """Ctrl+G — opens a live-updating view of the current subagent's
        task/tool-call history (SubagentActivityModal), or just reports
        there's nothing running yet, same message /subagent gives (also a
        toast, same reasoning)."""
        if self._activity.subagent is None:
            self.notify(_NO_SUBAGENT_RUNNING_MESSAGE)
            return
        self.app.push_screen(SubagentActivityModal(self._activity))

    def _handle_prune_tool_results_command(self, arg: str | None) -> None:
        """`/prune-tool-results [off|on|<n>]` — view/toggle/set the
        no-LLM-call tool-result pruning pass (see
        agent/context_pruning.py's prune_old_tool_results, run from
        _run_one_turn). No-arg reports the current enabled state and
        keep_recent_turns. `off`/`on` toggles prune_tool_results_enabled.
        A positive integer sets prune_tool_results_keep_recent_turns and
        implicitly re-enables it. Persisted to config.toml the same way
        /timeout persists request_timeout_s; applied live since
        _run_one_turn reads these settings fresh every turn. A toast, not a
        transcript message - same "glance and maybe retype" reasoning as
        /timeout above."""
        if not arg:
            state = "enabled" if self._settings.prune_tool_results_enabled else "disabled"
            self.notify(
                f"prune_tool_results is {state}, keeping the most recent "
                f"{self._settings.prune_tool_results_keep_recent_turns} turn(s) verbatim. "
                "Usage: /prune-tool-results off|on|<n>"
            )
            return

        if arg == "off":
            self._settings.prune_tool_results_enabled = False
            update_config_file(prune_tool_results_enabled=False)
            self.notify("prune_tool_results disabled.")
            return

        if arg == "on":
            self._settings.prune_tool_results_enabled = True
            update_config_file(prune_tool_results_enabled=True)
            self.notify("prune_tool_results enabled.")
            return

        try:
            keep_recent_turns = int(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't 'off', 'on', or a valid number.", severity="warning")
            return
        if keep_recent_turns <= 0:
            self.notify(
                "keep_recent_turns must be greater than 0 (use /prune-tool-results off to "
                "disable pruning entirely).",
                severity="warning",
            )
            return

        self._settings.prune_tool_results_enabled = True
        self._settings.prune_tool_results_keep_recent_turns = keep_recent_turns
        update_config_file(
            prune_tool_results_enabled=True,
            prune_tool_results_keep_recent_turns=keep_recent_turns,
        )
        self.notify(
            f"prune_tool_results_keep_recent_turns set to {keep_recent_turns}. "
            "Takes effect on the next turn."
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
        re-enables it. Persisted to config.toml, applied live. A toast, not
        a transcript message - same "glance and maybe retype" reasoning as
        /timeout above."""
        if not arg:
            state = "enabled" if self._settings.max_response_tokens_enabled else "disabled"
            current = compute_max_response_tokens(
                self._session,
                limit_table=self._context_limit_table,
                safety_margin=self._settings.max_response_tokens_safety_margin,
            )
            current_text = f"{current:,} tokens" if current is not None else "no cap (not yet computable)"
            self.notify(
                f"max_response_tokens is {state}, safety margin "
                f"{self._settings.max_response_tokens_safety_margin:,} tokens. Would currently "
                f"send max_tokens={current_text}. Usage: /max-response-tokens off|on|<margin>"
            )
            return

        if arg == "off":
            self._settings.max_response_tokens_enabled = False
            update_config_file(max_response_tokens_enabled=False)
            self.notify("max_response_tokens disabled.")
            return

        if arg == "on":
            self._settings.max_response_tokens_enabled = True
            update_config_file(max_response_tokens_enabled=True)
            self.notify("max_response_tokens enabled.")
            return

        try:
            safety_margin = int(arg)
        except ValueError:
            self.notify(f"'{arg}' isn't 'off', 'on', or a valid number.", severity="warning")
            return
        if safety_margin <= 0:
            self.notify(
                "safety margin must be greater than 0 (use /max-response-tokens off to "
                "disable the cap entirely).",
                severity="warning",
            )
            return

        self._settings.max_response_tokens_enabled = True
        self._settings.max_response_tokens_safety_margin = safety_margin
        update_config_file(
            max_response_tokens_enabled=True,
            max_response_tokens_safety_margin=safety_margin,
        )
        self.notify(
            f"max_response_tokens_safety_margin set to {safety_margin:,}. Takes effect on the "
            "next turn."
        )

    def _handle_rename_command(self, arg: str | None) -> None:
        """`/rename [name]` — sets Session.title, which derive_title() (used
        by the /sessions list) prefers over the auto-derived first-message
        snippet. No-arg shows the current title. A toast, not a transcript
        message - same "glance and maybe retype" reasoning as /timeout
        above."""
        if not arg:
            self.notify(f"Current session title: '{self._session.derive_title()}'. Usage: /rename <name>")
            return

        self._session.title = arg
        self._store.save(self._session)
        self.notify(f"Session renamed to '{arg}'.")

    def _handle_theme_command(self, arg: str | None) -> None:
        """`/theme [name]` — App.theme is the actual switch (Textual repaints
        every CSS design-token-based style live, no restart needed); this
        just persists the choice the same way /rename persists a title, so
        it's remembered next time (see PcliApp.on_mount, which applies
        settings.ui_theme at startup). No-arg lists every registered theme
        (Textual's own builtins plus pcli's vim-* ones from tui/themes.py),
        marking the active one — also the fallback shown for an unknown
        name, rather than silently doing nothing. The set-confirmation is a
        toast (same "glance and maybe retype" reasoning as /timeout above);
        the listing branches stay in the transcript since they dump the
        full theme list, same reasoning as /memory's/`/toolbox list`'s own
        listing output."""
        message_view = self.query_one(MessageView)
        available = sorted(self.app.available_themes)
        current = self.app.theme

        if arg and arg in self.app.available_themes:
            self.app.theme = arg
            self._settings.ui_theme = arg
            update_config_file(ui_theme=arg)
            self.notify(f"Theme set to '{arg}'.")
            return

        listing = "\n".join(f"- `{name}`{' (active)' if name == current else ''}" for name in available)
        prefix = f"Unknown theme '{arg}'.\n\n" if arg else ""
        message_view.add_message(
            "system", f"{prefix}**Available themes:**\n{listing}\n\nUsage: /theme <name>"
        )

    def _set_plan_mode(self, enabled: bool) -> None:
        """`/plan` (enter) / `/build` (exit) — restricts the agent to
        plan_mode_safe tools (read/explore only, no writes/edits/shell). The
        registry swap is the primary mechanism (the model never even sees a
        disallowed tool); AgentLoop's dispatch-time backstop and the
        per-turn prompt reinforcement (agent/prompt.py's
        PLAN_MODE_REINFORCEMENT, shared with headless.py's own plan-mode
        support) are the additional layers on top, not the boundary itself."""
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
            self.notify(
                "Usage: /toolbox discover <name> [path] | /toolbox list | /toolbox remove <name>",
                severity="warning",
            )

    @work(exclusive=True)
    async def _toolbox_discover(self, name: str, path: str | None = None) -> None:
        """Discovery's own failure and success summary stay in the
        transcript (genuine, potentially detailed reference content); the
        "isn't available" guard and the "Discovering..." progress ping are
        toasts - the ping is superseded within moments by one of the two
        transcript outcomes above, so keeping it around permanently would
        just be a stray duplicate."""
        message_view = self.query_one(MessageView)
        if self._toolbox_manager is None:
            self.notify("Toolbox isn't available (gateway/sandbox not set up).", severity="warning")
            return
        self.notify(f"Discovering '{name}'...")
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
        if self._toolbox_manager is None:
            self.notify("Toolbox isn't available (gateway/sandbox not set up).", severity="warning")
            return
        entries = self._toolbox_manager.list_discovered()
        if not entries:
            self.notify("No software discovered yet. Try /toolbox discover <name>.")
            return
        lines = [
            f"- **{name}** [{entry['source']}] {entry.get('version', '?')} - "
            f"{entry.get('tool_count', 0)} tool(s)"
            for name, entry in entries.items()
        ]
        self.query_one(MessageView).add_message("system", "\n".join(lines))

    @work(exclusive=True)
    async def _toolbox_remove(self, name: str) -> None:
        if self._toolbox_manager is None:
            self.notify("Toolbox isn't available (gateway/sandbox not set up).", severity="warning")
            return
        self._toolbox_manager.remove(name)
        self.notify(f"Removed '{name}' from the toolbox.")

    @work(exclusive=True)
    async def _handle_models_command(self, arg: str | None) -> None:
        """"Failed to list models" stays in the transcript as a real,
        investigable failure; everything else here is a toast - a quick
        confirmation or a state the user can just retry."""
        if arg:
            self._set_model(arg)
            self.notify(f"Model set to '{arg}'.")
            return

        if not self._settings.gateway_base_url:
            self.notify(
                "No gateway URL configured. Set PCLI_GATEWAY_URL and restart pcli.",
                severity="warning",
            )
            return

        client = self._client
        owns_client = client is None
        if client is None:
            client = GatewayClient(self._settings)

        try:
            models = await client.list_models()
        except GatewayError as exc:
            self.query_one(MessageView).add_message("system", f"Failed to list models: {exc.message}")
            return
        finally:
            if owns_client:
                await client.aclose()

        if not models:
            self.notify("Gateway returned no models.", severity="warning")
            return

        from pcli.tui.screens.models import ModelListScreen

        current = self._settings.default_model or self._session.model or None
        selected = await self.app.push_screen_wait(ModelListScreen(models, current))
        if selected:
            self._set_model(selected)
            self.notify(f"Model set to '{selected}'.")

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
        """A toast, not a transcript message - same "glance and maybe
        retype" reasoning as /timeout above; the exported path is right
        there in the notification and the file itself is the durable
        record, not this confirmation."""
        from pcli.config.paths import data_dir

        out_path = (
            Path(out_path_arg).expanduser()
            if out_path_arg
            else data_dir() / "exports" / f"{self._session.id}.pcli-session.json"
        )
        export_session(self._session, out_path, store=self._store)
        self.notify(f"Exported session to {out_path}")

    def _record_tool_invocation(self, event: ToolResultEvent) -> None:
        """Delegates to agent/runtime.py's record_tool_invocation, shared
        with headless/scheduled runs (run_headless_task) so every caller
        records identically - this method used to duplicate that logic
        TUI-only."""
        record_tool_invocation(
            self._session, event, audit_enabled=self._settings.audit_mode_enabled
        )

    def _effective_compaction_model(self) -> str | None:
        """The model compaction summarization and memory extraction should
        use - settings.compaction_model if configured (a deliberately
        cheaper/smaller model for these mechanical, lower-stakes background
        calls), falling back to the same default_model/session.model chain
        every other call site here uses when it isn't."""
        return (
            self._settings.compaction_model
            or self._settings.default_model
            or self._session.model
            or None
        )

    def _show_decision_notice(self, event: ToolResultEvent) -> None:
        """record_decision results are shown as a distinct, always-visible
        message (not the generic collapsed-by-default tool Collapsible) —
        the whole point of the decision log is that it's immediately
        scannable, not tucked away."""
        self._render_decision_notice(event.tool_call.function.arguments)

    def _render_decision_notice(self, arguments_json: str) -> None:
        """Shared with _replay_message_history, which reconstructs this same
        notice for a reloaded session from the original record_decision
        call's arguments rather than re-deriving it some other way."""
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            arguments = {}
        decision = arguments.get("decision", "") if isinstance(arguments, dict) else ""
        rationale = arguments.get("rationale", "") if isinstance(arguments, dict) else ""
        self.query_one(MessageView).add_message("decision", f"**{decision}**\n\n{rationale}")

    def _replay_message_history(self) -> None:
        """Rebuilds the visible transcript from session.messages using the
        same rendering helpers a live turn uses (add_tool_call/
        add_tool_result/_render_decision_notice) — a real reported gap:
        the previous reload logic only ever showed user/assistant .content
        and a flat, hard-truncated-to-500-chars line per tool result, so a
        tool-call-only assistant turn (content=None, tool_calls set) vanished
        entirely, tool results lost their syntax-highlighted/collapsible
        rendering, and a mid-conversation system message (notably a
        compaction summary, which replaces old turns in session.messages
        itself) wasn't shown at all. Purely cosmetic either way —
        session.messages itself (what's actually resent to the LLM) is
        untouched regardless of how it's displayed here.

        One real, unavoidable gap: whether a given past tool call actually
        errored isn't retained on Message itself (only in
        Session.tool_invocations, which - unlike session.messages - is never
        pruned/compacted, so it can't be reliably correlated back to a
        specific reconstructed message once either has happened). Every
        reconstructed tool result therefore renders as if it succeeded, even
        if the original call actually failed."""
        message_view = self.query_one(MessageView)
        messages = self._session.messages
        skip_leading_system_prompt = bool(messages) and messages[0].role == "system"
        pending_calls: dict[str, ToolCall] = {}

        for index, message in enumerate(messages):
            if index == 0 and skip_leading_system_prompt:
                continue
            if message.role in ("system", "user") and message.content:
                message_view.add_message(message.role, message.content)
            elif message.role == "assistant":
                if message.content:
                    message_view.add_message("assistant", message.content)
                for call in message.tool_calls or []:
                    pending_calls[call.id] = call
                    purpose = extract_purpose(call.function.arguments)
                    message_view.add_tool_call(
                        call.function.name, call.function.arguments, purpose=purpose
                    )
            elif message.role == "tool" and message.content:
                originating_call = pending_calls.pop(message.tool_call_id or "", None)
                if message.name == "record_decision" and originating_call is not None:
                    self._render_decision_notice(originating_call.function.arguments)
                else:
                    message_view.add_tool_result(message.name or "tool", message.content, is_error=False)

    async def _run_compaction(self, reason: str) -> None:
        """Summarizes and archives the oldest turns of session.messages (see
        agent/compaction.py) — either after an "auto" trigger from
        _stream_response, or a "manual" /compact command. Plain async method
        (not @work) so _stream_response can just await it directly while
        already inside its own worker; /compact reaches it via the small
        @work-wrapped _manual_compact below, same pattern as every other
        synchronously-dispatched command in this file. The "isn't
        available"/"still working"/"nothing to compact yet" guards below
        are toasts - trivially re-checked by rerunning /compact; a real
        failure and the actual success notice both stay in the transcript
        as genuine, investigable records."""
        message_view = self.query_one(MessageView)
        status_bar = self.query_one(StatusBar)
        if self._client is None or self._agent_loop is None:
            if reason == "manual":
                self.notify(
                    "Compaction isn't available (gateway/sandbox not set up).",
                    severity="warning",
                )
            return

        if reason == "manual" and self._turn_in_progress:
            # maybe_compact slices/replaces session.messages directly —
            # running it concurrently with an active turn (which also reads
            # and appends to that same list) could corrupt the conversation.
            # The "auto" trigger is never at risk of this: it only ever runs
            # sequentially, awaited from inside _run_one_turn itself, after
            # that turn has already finished.
            self.notify(
                "Still working on the current turn — try /compact again once it's done.",
                severity="warning",
            )
            return

        configured_keep_recent_turns = self._settings.auto_compact_keep_recent_turns
        tightened_to: int | None = None
        status_bar.busy = True
        try:
            # Regression guard: this used to only cover the maybe_compact
            # call(s) below, not the bookkeeping/memory-extraction that
            # follows a successful one - "Working..." would disappear the
            # instant summarization finished, then reappear once memory
            # extraction's own (invisible) gateway call finally completed,
            # looking exactly like pcli had silently stalled in between.
            # Covering the whole function in one busy=True/False window
            # fixes that: the indicator now stays lit for everything this
            # method actually does, not just its first step.
            try:
                result = await maybe_compact(
                    self._session,
                    gateway_client=self._client,
                    model=self._effective_compaction_model(),
                    artifact_store=self._artifact_store,
                    keep_recent_turns=configured_keep_recent_turns,
                )
                if result is None:
                    # The configured recent-turns window can itself be
                    # what's filling context - e.g. several truncation/
                    # auto-continue retries (each its own turn boundary,
                    # see compaction.py's turn_boundaries) concentrated
                    # inside the protected window, with everything older
                    # already compacted away by an earlier pass. Only retry
                    # with a smaller window when there's genuine pressure
                    # to free room - a small/fresh session at low usage
                    # should still just report "nothing to compact" rather
                    # than having its only turns eaten unnecessarily.
                    usage = current_context_usage(
                        self._session, limit_table=self._context_limit_table
                    )
                    if usage.fraction >= self._settings.auto_compact_threshold:
                        for smaller in range(configured_keep_recent_turns - 1, -1, -1):
                            result = await maybe_compact(
                                self._session,
                                gateway_client=self._client,
                                model=self._effective_compaction_model(),
                                artifact_store=self._artifact_store,
                                keep_recent_turns=smaller,
                            )
                            if result is not None:
                                tightened_to = smaller
                                break
            except GatewayError as exc:
                # Previously uncaught here: maybe_compact's one
                # summarization call failing (e.g. a timeout) would crash
                # straight out of this worker with nothing shown to the
                # user - the turn that triggered auto-compaction had
                # already completed and saved successfully by this point,
                # so this is a notice, not a lost turn, but it still needs
                # to be visible (context usage just silently won't have
                # shrunk).
                logger.exception("Gateway error during compaction: %s", exc.message)
                message_view.add_message("system", f"Compaction failed: {exc.message}")
                return

            if result is None:
                if reason == "manual":
                    self.notify("Nothing to compact yet.")
                return

            # Reuses ToolInvocation.full_result_ref purely so export_session
            # (which only bundles blobs it finds referenced there) carries
            # this artifact along too — no real tool call happened.
            self._session.tool_invocations.append(
                ToolInvocation(
                    tool_name="_compaction",
                    arguments={},
                    status="ok",
                    result_summary=f"Compacted {result.messages_compacted} message(s).",
                    full_result_ref=SessionArtifactStore.blob_name_for(result.artifact_id),
                )
            )
            # Real spend, so it counts toward cost — but tagged
            # source="compaction" so current_context_usage/
            # compute_max_response_tokens (which look for the last *main*-
            # conversation entry, not just the literal last one) aren't
            # misled into thinking the main conversation is however big
            # this one summarization call's own prompt happened to be. Also
            # deliberately not fed into _refresh_context_display, for the
            # same reason subagent usage isn't - the status bar
            # self-corrects on the next real turn's usage report.
            self._cost_tracker.record_turn(
                self._effective_compaction_model() or "",
                result.usage,
                source="compaction",
            )
            self._refresh_cost_display(status_bar)
            tightened_note = (
                f" (kept only the last {tightened_to} recent turn(s) verbatim instead of the "
                f"usual {configured_keep_recent_turns} — context was still full at that setting)"
                if tightened_to is not None
                else ""
            )
            message_view.add_message(
                "system",
                f"Compacted {result.messages_compacted} earlier message(s) to reduce context "
                f"usage (archived as artifact_id='{result.artifact_id}'){tightened_note}.",
            )
            self._store.save(self._session)

            if self._settings.memory_enabled:
                await self._extract_memory_from(result.artifact_id, status_bar)
        finally:
            status_bar.busy = False

    async def _extract_memory_from(self, artifact_id: str, status_bar: StatusBar) -> None:
        """Reviews the exact transcript maybe_compact just archived for
        anything durable/cross-session-worthy (memory/extraction.py) -
        piggybacked on compaction rather than a separate trigger, since a
        session substantial enough to need compacting is also substantial
        enough to be worth learning from, and this reuses the transcript
        already sitting in the artifact store instead of re-deriving it.
        Best-effort: a failure here is logged and otherwise invisible - it
        never touched the turn that triggered compaction, and losing one
        extraction pass isn't worth interrupting the user over."""
        transcript = self._artifact_store.get(artifact_id)
        if not transcript:
            return
        model = self._effective_compaction_model()
        try:
            extraction_ctx = replace(self._make_tool_context(), model=model)
            usages = await extract_memory(transcript, extraction_ctx)
        except GatewayError as exc:
            logger.exception("Gateway error during memory extraction: %s", exc.message)
            return
        if not usages:
            return
        for usage in usages:
            self._cost_tracker.record_turn(model or "", usage, source="memory")
        self._refresh_cost_display(status_bar)
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
                if self._queued_followups:
                    # The turn that just finished has already appended its
                    # own assistant reply to session.messages (turn_complete,
                    # inside _run_one_turn) - only now is it safe to append
                    # what the user typed while that was still in flight, so
                    # it lands after that reply instead of before it.
                    for text in self._queued_followups:
                        self._session.messages.append(Message(role="user", content=text))
                    self._queued_followups = []
                    self._store.save(self._session)
                    self._has_queued_followup = False
                    continue
                if not self._has_queued_followup:
                    break
                self._has_queued_followup = False
        finally:
            self._turn_in_progress = False

    async def _prune_and_maybe_auto_compact(self) -> None:
        """Cheap, mechanical, no-LLM-call pruning first (so compaction's own
        summarization call has less bulk to work with by the time its
        threshold is reached), then a real auto-compaction pass if context
        is still over auto_compact_threshold afterward. Factored out so the
        exact same recovery can run from _run_one_turn's GatewayError
        handler, not just its normal end-of-turn path — see that call site
        for why: a turn that fails outright because context was already
        full previously skipped this entirely (an early `return` inside the
        except block), so a session over threshold stayed over threshold and
        every subsequent turn failed the exact same way, with no compaction
        ever attempted despite the status bar having shown the overflow."""
        if self._settings.prune_tool_results_enabled:
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
        status_bar.main_tool_calls = 0
        status_bar.main_tool_calls_limit = self._permission_manager.guardrails.max_tool_calls_per_turn

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
        response_truncated = False

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
                chat_messages.append(ChatMessage(role="system", content=PLAN_MODE_REINFORCEMENT))
            async for chunk in self._agent_loop.run_turn(
                chat_messages,
                ask=ask,
                budget_check=lambda: cost_budget_reason(
                    self._session, self._settings.max_session_cost_usd
                ),
            ):
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
                    status_bar.main_tool_calls += 1
                    if chunk.tool_call.function.name == "write_todos":
                        self._refresh_todo_pane()
                    # source="subagent": real spend from a nested AgentLoop
                    # (spawn_subagent/explore_*/etc.), not the main
                    # conversation - see the "compaction" note above for why
                    # this tag matters, not just that it counts toward cost.
                    for extra in chunk.extra_usage:
                        self._cost_tracker.record_turn(
                            self._settings.default_model or self._session.model,
                            extra,
                            source="subagent",
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
                    response_truncated = chunk.response_truncated
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
            message_view.add_message(
                "system",
                f"Gateway error: {exc.message}\n\n"
                "**Note:** the message you just sent is still part of this session and will "
                "be resent as context on your next turn - a failed attempt doesn't discard "
                "it, it isn't silently skipped.",
            )
            self._store.save(self._session)
            # A GatewayError this far into a turn is often exactly a context-
            # length overflow (see llm/client.py's _http_status_hint) - the
            # status bar may well have shown context usage crossing 100% on
            # an earlier "usage" chunk of this same turn, right before the
            # next internal call got rejected for being too large. Without
            # this, the oversized history that caused the failure would
            # never get compacted (the normal end-of-turn call below is
            # never reached on this path), so every subsequent turn would
            # fail the exact same way. Safe to attempt unconditionally: it's
            # a no-op unless context is actually still over threshold, and
            # _run_compaction already handles its own GatewayError quietly.
            await self._prune_and_maybe_auto_compact()
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

        await self._prune_and_maybe_auto_compact()

        if response_truncated:
            # Deliberately after pruning/auto-compact above, not before: if
            # context was tight enough to truncate this response in the
            # first place, freeing some up first gives the retry an actual
            # chance of finishing instead of just hitting the same wall.
            self._consecutive_truncations += 1
            if self._consecutive_truncations <= _MAX_CONSECUTIVE_AUTO_CONTINUES:
                message_view.add_message(
                    "system",
                    "Response was cut off by the token limit before finishing — continuing "
                    f"automatically ({self._consecutive_truncations}/{_MAX_CONSECUTIVE_AUTO_CONTINUES}).",
                )
                self._session.messages.append(Message(role="user", content=_AUTO_CONTINUE_MESSAGE))
                message_view.add_message("user", _AUTO_CONTINUE_MESSAGE)
                self._store.save(self._session)
                self._has_queued_followup = True
            else:
                message_view.add_message(
                    "system",
                    f"Response was cut off by the token limit {_MAX_CONSECUTIVE_AUTO_CONTINUES} "
                    "times in a row — stopping automatic continuation rather than risk a runaway "
                    "loop. Try /max-response-tokens to raise the response-length cap, or just say "
                    '"continue" to keep going manually.',
                )
                self._consecutive_truncations = 0
        else:
            self._consecutive_truncations = 0
