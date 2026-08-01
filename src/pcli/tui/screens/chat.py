"""Main chat screen: streams a conversation against the configured gateway,
dispatching tool calls through permissions + sandbox, persisting to a Session
after every turn."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Input

from pcli.agent.loop import AgentLoop, ToolResultEvent
from pcli.agent.prompt import build_system_prompt
from pcli.config.settings import Settings, get_settings
from pcli.cost.context import ContextLimitTable, current_context_usage
from pcli.cost.tracker import CostTracker
from pcli.llm.client import GatewayClient
from pcli.llm.errors import GatewayError
from pcli.llm.models import Usage
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
from pcli.tui.widgets.status_bar import StatusBar

_TOOL_RESULT_PREVIEW_CHARS = 2000


class ChatScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+c", "quit", "Quit")]

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
        self._permission_manager = PermissionManager()
        self._sandbox: Sandbox | None = None
        self._tool_registry: ToolRegistry | None = None
        self._toolbox_manager: ToolboxManager | None = None
        self._current_ask: AskCallback | None = None
        self._cwd = Path.cwd()

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

        self._cost_tracker = CostTracker(self._session)
        self._context_limit_table = ContextLimitTable.load()
        self._artifact_store = SessionArtifactStore(self._store, self._session.id)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield MessageView(id="message-view")
            yield StatusBar(id="status-bar")
            yield Input(
                placeholder="Ask pcli... (/sessions, /export, /toolbox, !shell, !!quiet-shell)",
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

    async def on_mount(self) -> None:
        self.query_one(Input).focus()
        status_bar = self.query_one(StatusBar)
        status_bar.model = self._session.model or self._settings.default_model
        self._refresh_cost_display(status_bar)
        context_usage = current_context_usage(self._session, limit_table=self._context_limit_table)
        status_bar.context_used_tokens = context_usage.used_tokens
        status_bar.context_limit_tokens = context_usage.limit_tokens

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

        if not self._settings.is_configured():
            message_view.add_message(
                "system",
                "Gateway not configured. Set `PCLI_GATEWAY_URL` and `PCLI_GATEWAY_API_KEY` "
                "(or edit the config file) and restart pcli.",
            )
            return

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
            max_tool_iterations=self._settings.max_tool_iterations,
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
            max_tool_iterations=self._settings.max_tool_iterations,
            session=self._session,
            artifact_store=self._artifact_store,
        )

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

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
        self._stream_response()

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
        else:
            message_view.add_message("system", f"Unknown command: /{command}")

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
        except ToolboxDiscoveryError as exc:
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

    @work(exclusive=True)
    async def _stream_response(self) -> None:
        assert self._agent_loop is not None
        message_view = self.query_one(MessageView)
        status_bar = self.query_one(StatusBar)
        message_view.add_message("assistant", "")

        async def ask(tool_name: str, arguments: dict, risk_description: str):
            return await ask_via_modal(self.app, tool_name, arguments, risk_description)

        self._current_ask = ask

        try:
            chat_messages = [m.to_chat_message() for m in self._session.messages]
            async for chunk in self._agent_loop.run_turn(chat_messages, ask=ask):
                if chunk.kind == "text_delta":
                    message_view.append_to_last(chunk.text)
                elif chunk.kind == "usage":
                    self._cost_tracker.record_turn(
                        self._settings.default_model or self._session.model, chunk.usage
                    )
                    self._refresh_cost_display(status_bar)
                    self._refresh_context_display(status_bar, chunk.usage)
                elif chunk.kind == "tool_start":
                    message_view.finish_streaming()
                    message_view.add_message(
                        "tool",
                        f"→ {chunk.tool_call.function.name}({chunk.tool_call.function.arguments})",
                    )
                    message_view.finish_streaming()
                elif chunk.kind == "tool_result":
                    self._record_tool_invocation(chunk)
                    for extra in chunk.extra_usage:
                        self._cost_tracker.record_turn(
                            self._settings.default_model or self._session.model, extra
                        )
                    if chunk.extra_usage:
                        self._refresh_cost_display(status_bar)
                    preview = chunk.output[:_TOOL_RESULT_PREVIEW_CHARS]
                    if len(chunk.output) > _TOOL_RESULT_PREVIEW_CHARS:
                        preview += "\n[...truncated in view; full output saved to session...]"
                    message_view.add_message("tool", ("[error] " if chunk.is_error else "") + preview)
                    message_view.finish_streaming()
                elif chunk.kind == "turn_complete":
                    self._session.messages.extend(
                        Message.from_chat_message(m) for m in chunk.new_messages
                    )
        except GatewayError as exc:
            message_view.finish_streaming()
            message_view.add_message("system", f"Gateway error: {exc.message}")
            return
        message_view.finish_streaming()
        self._store.save(self._session)
