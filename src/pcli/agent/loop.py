"""Turn orchestration: message history -> gateway stream -> tool dispatch ->
gateway stream (repeat) -> caller.

The dispatch loop is deliberately decoupled from sessions/TUI: it consumes a
plain message list and an `ask` callback (see permissions/manager.py), and
yields events the caller renders and, in TurnCompleteEvent, folds back into
its own persisted message history.

It's also where large tool results get truncated out of the conversation
and archived to the artifact library (see tools/artifacts.py) — a single
choke point that every tool's output passes through, rather than each tool
having to implement its own truncation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Literal

import jsonschema
from pydantic import BaseModel, Field

from pcli.llm.client import GatewayClient
from pcli.llm.models import (
    ChatMessage,
    FinishEvent,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallCompleteEvent,
    Usage,
)

_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})
"""What different OpenAI-compatible gateways send as finish_reason when a
response was cut off by hitting the token cap mid-generation, rather than
the model choosing to stop - "length" is the official OpenAI value; "max_
tokens" covers at least one observed local-gateway variant. Checked
case-sensitively against the raw value, same as every other finish_reason
comparison in this codebase (llm/streaming.py's own "tool_calls" check)."""
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.tools.base import ToolContext
from pcli.tools.registry import ToolRegistry

_DEFAULT_ARTIFACT_THRESHOLD_CHARS = 4000
_ARTIFACT_PREVIEW_CHARS = 2000
_MAX_IDENTICAL_TOOL_CALL_REPEATS = 3
"""Confirmed against a real debugged session: a heavily-quantized local
model called run_shell with byte-identical arguments 12 times in a row,
getting the exact same (unhelpful) result each time, before finally trying
something else — the system prompt's own "Recovering from a failed tool
call" guidance already says a third near-identical retry is never the right
move, but that's a suggestion the model has to choose to follow. This is
the mechanical backstop: independent of whether the model notices or
complies, pcli itself refuses to run an exact repeat a third time — see
_dispatch_tool_call's own repeat-tracking below."""


class ToolStartEvent(BaseModel):
    kind: Literal["tool_start"] = "tool_start"
    tool_call: ToolCall


class ToolResultEvent(BaseModel):
    kind: Literal["tool_result"] = "tool_result"
    tool_call: ToolCall
    output: str
    is_error: bool = False
    extra_usage: list[Usage] = Field(default_factory=list)
    artifact_id: str | None = None
    """Set when `output` is a truncated preview whose full content was
    archived — the id fetch_artifact needs to retrieve the rest."""


class TurnCompleteEvent(BaseModel):
    kind: Literal["turn_complete"] = "turn_complete"
    new_messages: list[ChatMessage]
    terminated_early: bool = False
    """True if the turn was cut off by max_tool_iterations or the
    max_tool_calls_per_turn guardrail rather than the model choosing to
    stop on its own - the model's work here is genuinely incomplete, not
    just finished. Consumers that treat a turn's outcome as a result (namely
    spawn_subagent/make_agent_tool reporting back to a parent loop) use this
    to mark that result as an error instead of a normal completion."""
    response_truncated: bool = False
    """True if the turn's final (no-tool-call) response was cut off by
    hitting the token/length cap mid-generation (finish_reason in
    _TRUNCATION_FINISH_REASONS) rather than the model actually finishing -
    distinct from terminated_early above (a different cause: a guardrail
    stopping an otherwise-healthy turn, not the model getting cut off
    mid-sentence). A real observed failure this exists to let a caller
    detect: the model says "Let me implement X:" and stops there with no
    tool call, because it ran out of room to actually make one - previously
    indistinguishable from a deliberate, complete stop, so the turn just
    silently ended with genuinely unfinished work and no explanation."""


AgentEvent = StreamEvent | ToolStartEvent | ToolResultEvent | TurnCompleteEvent

ToolContextFactory = Callable[[], ToolContext]


class AgentLoop:
    def __init__(
        self,
        gateway_client: GatewayClient,
        *,
        model: str | None = None,
        tool_registry: ToolRegistry | None = None,
        permission_manager: PermissionManager | None = None,
        tool_context_factory: ToolContextFactory | None = None,
        max_tool_iterations: int | None = 25,
        artifact_threshold_chars: int = _DEFAULT_ARTIFACT_THRESHOLD_CHARS,
        max_response_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        """`max_tool_iterations=None` means unlimited (local-api mode).
        `max_response_tokens=None` means no cap is sent (the gateway's own
        default applies) — see set_max_response_tokens. `temperature=None`
        means no temperature field is sent at all (the gateway/model's own
        default applies) — see set_temperature."""
        self._client = gateway_client
        self._model = model
        self._tool_registry = tool_registry
        self._permission_manager = permission_manager
        self._tool_context_factory = tool_context_factory
        self._max_tool_iterations = max_tool_iterations
        self._artifact_threshold_chars = artifact_threshold_chars
        self._max_response_tokens = max_response_tokens
        self._temperature = temperature
        # Identical-tool-call repeat tracking (see _MAX_IDENTICAL_TOOL_CALL_
        # REPEATS above) - reset at the start of every run_turn so a fresh
        # user turn never inherits a stale count from an unrelated one.
        self._last_tool_call_signature: str | None = None
        self._identical_tool_call_repeats: int = 0

    @property
    def model(self) -> str | None:
        return self._model

    def set_model(self, model: str | None) -> None:
        self._model = model

    @property
    def max_response_tokens(self) -> int | None:
        """Read side of set_max_response_tokens — lets a caller building a
        nested AgentLoop for a subagent (spawn_subagent, agent_tools.py's
        make_agent_tool) inherit the parent's current cap instead of the
        subagent silently running with none at all."""
        return self._max_response_tokens

    @property
    def temperature(self) -> float | None:
        """Read side of set_temperature — same inheritance purpose as
        max_response_tokens above."""
        return self._temperature

    def set_tool_registry(self, tool_registry: ToolRegistry | None) -> None:
        self._tool_registry = tool_registry

    def set_max_tool_iterations(self, max_tool_iterations: int | None) -> None:
        """None means unlimited (local-api mode) — see __init__."""
        self._max_tool_iterations = max_tool_iterations

    def set_artifact_threshold_chars(self, artifact_threshold_chars: int) -> None:
        self._artifact_threshold_chars = artifact_threshold_chars

    def set_max_response_tokens(self, max_response_tokens: int | None) -> None:
        """The dynamic per-request max_tokens cap (cost/context.py's
        compute_max_response_tokens) — recomputed and set by the caller
        (ChatScreen) once per user-submitted turn, from live session usage
        AgentLoop itself has no access to (it's deliberately decoupled from
        sessions/TUI — see the module docstring). Applied to every
        chat_stream call this run_turn makes, including tool-call
        round-trips within the same turn, so it doesn't shrink further as
        those round-trips add their own usage — a reasonable simplification
        given the bug this fixes (a single very long response, not a
        many-tool-call turn) rather than a live per-call recomputation."""
        self._max_response_tokens = max_response_tokens

    def set_temperature(self, temperature: float | None) -> None:
        """None means no temperature field is sent at all — see __init__."""
        self._temperature = temperature

    async def run_turn(
        self,
        messages: list[ChatMessage],
        *,
        ask: AskCallback | None = None,
        budget_check: Callable[[], str | None] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """budget_check, if given, is called at the top of every loop
        iteration (same spot as the max_tool_iterations check below) -
        returning None means proceed, a reason string means stop the turn
        the same way max_tool_iterations does. A plain injected callable
        (not a raw Session/cap value) rather than importing Session here -
        matches the existing ask/tool_context_factory injection pattern and
        keeps this module decoupled from what a "budget" even is (see the
        module docstring); the caller closes over its own Session/Settings
        to build it - see cost/tracker.py's cost_budget_reason."""
        tools = self._tool_registry.to_openai_tools() if self._tool_registry else None
        working_messages = list(messages)
        original_len = len(working_messages)
        iterations = 0
        self._last_tool_call_signature = None
        self._identical_tool_call_repeats = 0
        tool_calls_dispatched = 0
        max_tool_calls_per_turn = (
            self._permission_manager.guardrails.max_tool_calls_per_turn
            if self._permission_manager is not None
            else None
        )
        terminated_early = False
        response_truncated = False

        while True:
            iterations += 1
            if budget_check is not None:
                budget_reason = budget_check()
                if budget_reason is not None:
                    note = f"\n[pcli] {budget_reason}"
                    yield TextDelta(text=note)
                    working_messages.append(ChatMessage(role="assistant", content=note))
                    terminated_early = True
                    break
            if self._max_tool_iterations is not None and iterations > self._max_tool_iterations:
                note = (
                    f"\n[pcli] Reached the max tool-call iteration limit "
                    f"({self._max_tool_iterations}) for this turn. If the model legitimately "
                    "needs more tool calls to finish, raise max_tool_iterations via "
                    "PCLI_MAX_TOOL_ITERATIONS or config.toml (--local-api removes this cap "
                    "entirely for a local gateway)."
                )
                yield TextDelta(text=note)
                working_messages.append(ChatMessage(role="assistant", content=note))
                terminated_early = True
                break

            text_parts: list[str] = []
            tool_calls_collected: list[ToolCall] = []
            finish_reason: str | None = None
            async for event in self._client.chat_stream(
                working_messages, model=self._model, tools=tools,
                max_tokens=self._max_response_tokens, temperature=self._temperature,
            ):
                if event.kind == "text_delta":
                    text_parts.append(event.text)
                if isinstance(event, ToolCallCompleteEvent):
                    tool_calls_collected = event.tool_calls
                if isinstance(event, FinishEvent):
                    finish_reason = event.reason
                yield event

            assistant_text = "".join(text_parts) or None

            if not tool_calls_collected:
                working_messages.append(ChatMessage(role="assistant", content=assistant_text))
                response_truncated = finish_reason in _TRUNCATION_FINISH_REASONS
                break

            working_messages.append(
                ChatMessage(role="assistant", content=assistant_text, tool_calls=tool_calls_collected)
            )

            limit_hit = False
            for call in tool_calls_collected:
                yield ToolStartEvent(tool_call=call)
                if (
                    max_tool_calls_per_turn is not None
                    and max_tool_calls_per_turn > 0  # <= 0 means unlimited (local-api mode)
                    and tool_calls_dispatched >= max_tool_calls_per_turn
                ):
                    # Still respond to every tool_call_id in this batch (required
                    # by the chat-completions protocol) rather than executing it.
                    denial = (
                        f"Denied: reached the guardrail limit of {max_tool_calls_per_turn} "
                        "tool call(s) for this turn."
                    )
                    output, is_error, extra_usage, artifact_id = denial, True, [], None
                    limit_hit = True
                else:
                    output, is_error, extra_usage, artifact_id = await self._dispatch_tool_call(
                        call, ask=ask
                    )
                    tool_calls_dispatched += 1
                working_messages.append(
                    ChatMessage(
                        role="tool", tool_call_id=call.id, name=call.function.name, content=output
                    )
                )
                yield ToolResultEvent(
                    tool_call=call,
                    output=output,
                    is_error=is_error,
                    extra_usage=extra_usage,
                    artifact_id=artifact_id,
                )

            if limit_hit:
                note = (
                    f"\n[pcli] Reached the guardrail limit of {max_tool_calls_per_turn} tool "
                    "call(s) for this turn. If this is expected, raise limits."
                    "max_tool_calls_per_turn in guardrails.toml (--local-api removes this cap "
                    "entirely for a local gateway)."
                )
                yield TextDelta(text=note)
                working_messages.append(ChatMessage(role="assistant", content=note))
                terminated_early = True
                break

        yield TurnCompleteEvent(
            new_messages=working_messages[original_len:],
            terminated_early=terminated_early,
            response_truncated=response_truncated,
        )

    def _archive_if_large(self, output: str, ctx: ToolContext) -> tuple[str, str | None]:
        if len(output) <= self._artifact_threshold_chars or ctx.artifact_store is None:
            return output, None

        artifact_id = ctx.artifact_store.put(output)
        preview_chars = min(_ARTIFACT_PREVIEW_CHARS, self._artifact_threshold_chars)
        preview = output[:preview_chars]
        truncated = (
            f"{preview}\n\n[...output truncated: {len(output)} chars total, archived as "
            f"artifact_id='{artifact_id}'. Call fetch_artifact(artifact_id='{artifact_id}') "
            "if you need the rest...]"
        )
        return truncated, artifact_id

    async def _dispatch_tool_call(
        self, call: ToolCall, *, ask: AskCallback | None
    ) -> tuple[str, bool, list[Usage], str | None]:
        tool = self._tool_registry.get(call.function.name) if self._tool_registry else None
        if tool is None:
            return f"Unknown tool: {call.function.name}", True, [], None

        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"Invalid arguments JSON: {exc}", True, [], None
        if not isinstance(arguments, dict):
            return "Tool arguments must be a JSON object.", True, [], None

        # "purpose" only exists in the advertised schema (see
        # ToolSpec.to_openai_tool) for the model's own benefit - it's never
        # part of a tool's real parameters, so it must never reach schema
        # validation or the handler. Popped from this parsed copy only; the
        # original call.function.arguments JSON string (as persisted in the
        # session's assistant message) is left untouched, which is what lets
        # context_pruning.py re-extract it later for a pruned placeholder.
        arguments.pop("purpose", None)

        # Mechanical backstop for a real observed failure mode (see
        # _MAX_IDENTICAL_TOOL_CALL_REPEATS above): a model that keeps
        # resending the exact same call, byte-for-byte, expecting a
        # different result. Signature is built from the same
        # purpose-stripped `arguments` dict everything below uses, so
        # changing only the "purpose" explanation while repeating the same
        # real action still counts as a repeat. Checked before the
        # permission gate specifically so an already-decided identical call
        # can't re-trigger another interactive prompt for the same thing.
        signature = f"{tool.name}:{json.dumps(arguments, sort_keys=True)}"
        if signature == self._last_tool_call_signature:
            self._identical_tool_call_repeats += 1
        else:
            self._last_tool_call_signature = signature
            self._identical_tool_call_repeats = 1
        if self._identical_tool_call_repeats >= _MAX_IDENTICAL_TOOL_CALL_REPEATS:
            blocked_message = (
                f"Blocked: this exact {tool.name} call (identical arguments) has now been "
                f"attempted {self._identical_tool_call_repeats} times in a row with no change "
                "in between — pcli is refusing to run it again, since repeating it will not "
                "produce a different result. Stop and change strategy: re-read the actual "
                "output from the previous attempts above, diagnose why it didn't help, and try "
                "something meaningfully different — or use ask_user_question if you're stuck."
            )
            return blocked_message, True, [], None

        try:
            jsonschema.validate(arguments, tool.parameters)
        except jsonschema.ValidationError as exc:
            return f"Arguments failed schema validation: {exc.message}", True, [], None

        if self._permission_manager is None:
            return "No permission manager configured; tool execution is disabled.", True, [], None

        if self._tool_context_factory is None:
            return "No tool execution context configured.", True, [], None
        # Built before the permission check (constructing it is side-effect
        # free) so check() can attach a PermissionGrant to ctx.session when
        # the user picks "remember for session/always".
        ctx = self._tool_context_factory()

        # Defense in depth: the registry not exposing a tool is necessary but
        # not sufficient on its own — this is the real backstop against a
        # stale/hallucinated tool call slipping through while plan mode is
        # active, independent of whatever registry happens to be wired up.
        if ctx.plan_mode and not tool.plan_mode_safe:
            return "Denied: not available in plan mode.", True, [], None

        command = arguments.get(tool.guardrail_command_arg) if tool.guardrail_command_arg else None
        path = arguments.get(tool.guardrail_path_arg) if tool.guardrail_path_arg else None
        python_module = (
            arguments.get(tool.guardrail_python_module_arg)
            if tool.guardrail_python_module_arg
            else None
        )
        decision, deny_reason = await self._permission_manager.check_with_reason(
            tool.name,
            arguments,
            command=command,
            path=path,
            python_module=python_module,
            ask=ask,
            risk_description=tool.risk_description,
            default_allow=not tool.needs_permission,
            session=ctx.session,
        )
        if decision == "deny":
            message = f"Permission denied: {deny_reason}." if deny_reason else "Permission denied."
            return message, True, [], None

        try:
            result = await tool.handler(arguments, ctx)
        except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
            return f"Tool raised an exception: {exc}", True, [], None

        # Global backstop on top of each tool's own (smaller) internal cap —
        # guardrails.max_output_bytes is meant to bound every tool uniformly,
        # not just the ones that happen to implement their own limit.
        raw_output = result.output
        max_output_bytes = self._permission_manager.guardrails.max_output_bytes
        if len(raw_output) > max_output_bytes:
            raw_output = (
                f"{raw_output[:max_output_bytes]}\n"
                f"[...output truncated to the guardrail limit of {max_output_bytes} chars...]"
            )

        output, artifact_id = self._archive_if_large(raw_output, ctx)
        return output, result.is_error, result.extra_usage, artifact_id
