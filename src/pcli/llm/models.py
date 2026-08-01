"""Pydantic models for the OpenAI-compatible chat-completions wire format."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


class ToolCallFunction(BaseModel):
    name: str
    arguments: str = ""


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = [tc.model_dump() for tc in self.tool_calls]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data


class ToolDefinition(BaseModel):
    """A single entry in the OpenAI-style `tools=[...]` request payload."""

    type: Literal["function"] = "function"
    function: dict[str, Any]


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    estimated: bool = False


class TextDelta(BaseModel):
    kind: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallDelta(BaseModel):
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


class ToolCallCompleteEvent(BaseModel):
    kind: Literal["tool_call_complete"] = "tool_call_complete"
    tool_calls: list[ToolCall]


class UsageEvent(BaseModel):
    kind: Literal["usage"] = "usage"
    usage: Usage


class FinishEvent(BaseModel):
    kind: Literal["finish"] = "finish"
    reason: str | None = None


StreamEvent = TextDelta | ToolCallDelta | ToolCallCompleteEvent | UsageEvent | FinishEvent
