"""Main chat screen: streams a conversation against the configured gateway,
dispatching tool calls through permissions + sandbox, persisting to a Session
after every turn."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Input

from pcli.agent.activity import ActivityTracker
from pcli.agent.compaction import maybe_compact
from pcli.agent.loop import AgentLoop, ToolResultEvent
from pcli.agent.prompt import build_system_prompt
from pcli.config.settings import Settings, get_settings, update_config_file
from pcli.cost.context import ContextLimitTable, current_context_usage
from pcli.cost.pricing_table import ModelPricing, PricingTable
from pcli.cost.tracker import CostTracker
from pcli.llm.client import GatewayClient
from pcli.llm.errors import GatewayError
from pcli.llm.models import Usage
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.sandbox.base import Sandbox
from pcli.sandbox.selector import select_sandbox
from pcli.session.export import export_session
from pcli.session.models import Message, Session, ToolInvocation
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import ToolContext
from pcli.tools.registry import ToolRegistry, build_default_registry
from pcli.tools.toolbox.manager import ToolboxDiscoveryError, ToolboxManager
from pcli.tui.screens.permission_modal import ask_via_modal
from pcli.tui.shell_passthrough import run_passthrough_command
from pcli.tui.widgets.message_view import MessageView
from pcli.tui.widgets.paste_input import PasteInput
from pcli.tui.widgets.status_bar import StatusBar
from pcli.tui.widgets.status_pane import StatusPane

# Used for local-api-mode sessions: always $0, regardless of pricing.toml or
# the builtin table — a local model's name could otherwise coincidentally
# match a paid pattern there (e.g. "llama-3*") and show a fake nonzero cost.
_FREE_PRICING_TABLE = PricingTable(entries={}, default=ModelPricing())

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
    BINDINGS: ClassVar[list[BindingType]] = []

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
        self._cwd = Path.cwd()
        self._activity = ActivityTracker()
        # Lets a message submitted while a turn is already running be
        # queued and processed right after, instead of either being ignored
        # or (the bug this fixes) silently cancelling the in-flight turn —
        # see _stream_response/on_input_submitted.
        self._turn_in_progress = False
        self._has_queued_followup = False

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
        self._artifact_store = SessionArtifactStore(self._store, self._session.id)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield StatusPane(id="status-pane")
            yield MessageView(id="message-view")
            yield StatusBar(id="status-bar")
            yield PasteInput(
                placeholder="Ask pcli... (/sessions, /export, /toolbox, /models, /compact, "
                "/timeout, !shell, !!quiet-shell, !!!interactive)",
                id="input-box",
                expand_full_paste=True,
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
        self.query_one(Input).focus()
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

        self._client = GatewayClient(self._settings)
        self._agent_loop = AgentLoop(
            self._client,
            model=self._settings.default_model or None,
            tool_registry=self._tool_registry,
            permission_manager=self._permission_manager,
            tool_context_factory=self._make_tool_context,
            max_tool_iterations=self._effective_max_tool_iterations(),
            artifact_threshold_chars=self._settings.artifact_threshold_chars,
        )

    def _make_tool_context(self) -> ToolContext:
        assert self._sandbox is not None
        return ToolContext(
            sandbox=self._sandbox,
            guardrails=self._permission_manager.guardrails,
            cwd=self._cwd,
            gateway_client=self._client,
            model=self._settings.default_model or None,
            tool_registry=self._tool_registry,
            permission_manager=self._permission_manager,
            ask=self._current_ask,
            max_tool_iterations=self._effective_max_tool_iterations(),
            session=self._session,
            artifact_store=self._artifact_store,
            activity=self._activity,
        )

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if isinstance(event.input, PasteInput):
            text = event.input.consume_pending_paste(text)
        if not text:
            return
        event.input.value = ""

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

            self.app.push_screen(SessionListScreen(self._store))
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

    def _handle_toolbox_command(self, rest: str) -> None:
        message_view = self.query_one(MessageView)
        sub, _, arg = rest.partition(" ")
        arg = arg.strip()

        if sub == "discover" and arg:
            self._toolbox_discover(arg)
        elif sub == "list":
            self._toolbox_list()
        elif sub == "remove" and arg:
            self._toolbox_remove(arg)
        else:
            message_view.add_message(
                "system",
                "Usage: /toolbox discover <name> | /toolbox list | /toolbox remove <name>",
            )

    @work(exclusive=True)
    async def _toolbox_discover(self, name: str) -> None:
        message_view = self.query_one(MessageView)
        if self._toolbox_manager is None:
            message_view.add_message("system", "Toolbox isn't available (gateway/sandbox not set up).")
            return
        message_view.add_message("system", f"Discovering '{name}'...")
        try:
            summary = await self._toolbox_manager.discover(
                name, gateway_client=self._client, model=self._settings.default_model or None
            )
        except (ToolboxDiscoveryError, GatewayError) as exc:
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

        self._current_ask = ask
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

        try:
            chat_messages = [m.to_chat_message() for m in self._session.messages]
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
                elif chunk.kind == "tool_start":
                    had_any_content = True
                    flush_reasoning()
                    message_view.finish_streaming()
                    message_view.add_message(
                        "tool",
                        f"→ {chunk.tool_call.function.name}({chunk.tool_call.function.arguments})",
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
            message_view.add_message(
                "system",
                f"The model didn't produce a reply or tool call this turn{suffix}. "
                "Try again, or ask something more focused.",
            )
        self._store.save(self._session)

        if self._settings.auto_compact_enabled:
            usage = current_context_usage(self._session, limit_table=self._context_limit_table)
            if usage.fraction >= self._settings.auto_compact_threshold:
                await self._run_compaction("auto")
