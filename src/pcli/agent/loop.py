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
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallCompleteEvent,
    Usage,
)
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.tools.base import ToolContext
from pcli.tools.registry import ToolRegistry

_DEFAULT_ARTIFACT_THRESHOLD_CHARS = 4000
_ARTIFACT_PREVIEW_CHARS = 2000


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
        max_tool_iterations: int = 25,
        artifact_threshold_chars: int = _DEFAULT_ARTIFACT_THRESHOLD_CHARS,
    ) -> None:
        self._client = gateway_client
        self._model = model
        self._tool_registry = tool_registry
        self._permission_manager = permission_manager
        self._tool_context_factory = tool_context_factory
        self._max_tool_iterations = max_tool_iterations
        self._artifact_threshold_chars = artifact_threshold_chars

    async def run_turn(
        self, messages: list[ChatMessage], *, ask: AskCallback | None = None
    ) -> AsyncIterator[AgentEvent]:
        tools = self._tool_registry.to_openai_tools() if self._tool_registry else None
        working_messages = list(messages)
        original_len = len(working_messages)
        iterations = 0

        while True:
            iterations += 1
            if iterations > self._max_tool_iterations:
                note = "\n[pcli] Reached the max tool-call iteration limit for this turn."
                yield TextDelta(text=note)
                working_messages.append(ChatMessage(role="assistant", content=note))
                break

            text_parts: list[str] = []
            tool_calls_collected: list[ToolCall] = []
            async for event in self._client.chat_stream(
                working_messages, model=self._model, tools=tools
            ):
                if event.kind == "text_delta":
                    text_parts.append(event.text)
                if isinstance(event, ToolCallCompleteEvent):
                    tool_calls_collected = event.tool_calls
                yield event

            assistant_text = "".join(text_parts) or None

            if not tool_calls_collected:
                working_messages.append(ChatMessage(role="assistant", content=assistant_text))
                break

            working_messages.append(
                ChatMessage(role="assistant", content=assistant_text, tool_calls=tool_calls_collected)
            )

            for call in tool_calls_collected:
                yield ToolStartEvent(tool_call=call)
                output, is_error, extra_usage, artifact_id = await self._dispatch_tool_call(
                    call, ask=ask
                )
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

        yield TurnCompleteEvent(new_messages=working_messages[original_len:])

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

        try:
            jsonschema.validate(arguments, tool.parameters)
        except jsonschema.ValidationError as exc:
            return f"Arguments failed schema validation: {exc.message}", True, [], None

        if self._permission_manager is None:
            return "No permission manager configured; tool execution is disabled.", True, [], None

        command = arguments.get(tool.guardrail_command_arg) if tool.guardrail_command_arg else None
        path = arguments.get(tool.guardrail_path_arg) if tool.guardrail_path_arg else None
        python_module = (
            arguments.get(tool.guardrail_python_module_arg)
            if tool.guardrail_python_module_arg
            else None
        )
        decision = await self._permission_manager.check(
            tool.name,
            arguments,
            command=command,
            path=path,
            python_module=python_module,
            ask=ask,
            risk_description=tool.risk_description,
            default_allow=not tool.needs_permission,
        )
        if decision == "deny":
            return "Permission denied.", True, [], None

        if self._tool_context_factory is None:
            return "No tool execution context configured.", True, [], None
        ctx = self._tool_context_factory()

        try:
            result = await tool.handler(arguments, ctx)
        except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
            return f"Tool raised an exception: {exc}", True, [], None

        output, artifact_id = self._archive_if_large(result.output, ctx)
        return output, result.is_error, result.extra_usage, artifact_id
