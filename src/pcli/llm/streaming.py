"""Parses an OpenAI-compatible chat-completions SSE stream into StreamEvents.

Tool-call arguments arrive fragmented across multiple chunks, keyed by the
`index` field within `delta.tool_calls`; fragments must be accumulated until
`finish_reason == "tool_calls"` before the full arguments JSON is usable.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from pcli.llm.models import (
    FinishEvent,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallCompleteEvent,
    ToolCallDelta,
    ToolCallFunction,
    Usage,
    UsageEvent,
)


class _ToolCallAccumulator:
    def __init__(self) -> None:
        self.id: str | None = None
        self.name: str | None = None
        self.arguments = ""

    def to_tool_call(self) -> ToolCall:
        return ToolCall(
            id=self.id or "",
            function=ToolCallFunction(name=self.name or "", arguments=self.arguments),
        )


async def parse_sse_stream(lines: AsyncIterator[str]) -> AsyncIterator[StreamEvent]:
    accumulators: dict[int, _ToolCallAccumulator] = {}

    async for raw_line in lines:
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        usage = chunk.get("usage")
        if usage:
            yield UsageEvent(
                usage=Usage(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                )
            )

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if content:
            yield TextDelta(text=content)

        # Reasoning/"thinking" models (DeepSeek-R1-style, Nemotron "detailed
        # thinking", ...) served through vLLM/SGLang/LM Studio stream their
        # chain-of-thought under a separate field, never under `content` —
        # a model that spends its whole response reasoning without ever
        # transitioning to `content` previously vanished here entirely: no
        # text, no tool call, no error, just a silently empty turn. Servers
        # aren't consistent about the key name, so both are checked.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            yield ReasoningDelta(text=reasoning)

        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            acc = accumulators.setdefault(index, _ToolCallAccumulator())
            if tc.get("id"):
                acc.id = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc.name = fn["name"]
            frag = fn.get("arguments") or ""
            if frag:
                acc.arguments += frag
            yield ToolCallDelta(
                index=index, id=tc.get("id"), name=fn.get("name"), arguments_fragment=frag
            )

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            if finish_reason == "tool_calls" and accumulators:
                completed = [accumulators[i].to_tool_call() for i in sorted(accumulators)]
                yield ToolCallCompleteEvent(tool_calls=completed)
                accumulators.clear()
            yield FinishEvent(reason=finish_reason)
