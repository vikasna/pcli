"""Unit coverage for parse_sse_stream, in particular reasoning_content/
reasoning fields: reasoning/"thinking" models (DeepSeek-R1-style, Nemotron
"detailed thinking", ...) served through OpenAI-compatible endpoints stream
their chain-of-thought under a channel separate from `delta.content` — this
was previously dropped entirely, silently, with no trace anywhere."""

import json
from collections.abc import AsyncIterator

import pytest

from pcli.llm.models import FinishEvent, ReasoningDelta, TextDelta, UsageEvent
from pcli.llm.streaming import parse_sse_stream


async def _lines(*chunks: dict) -> AsyncIterator[str]:
    for chunk in chunks:
        yield f"data: {json.dumps(chunk)}"
    yield "data: [DONE]"


@pytest.mark.asyncio
async def test_reasoning_content_field_yields_reasoning_delta():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "Let me think..."}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "The answer is 4."}, "finish_reason": "stop"}]},
    ]
    events = [e async for e in parse_sse_stream(_lines(*chunks))]

    reasoning = [e for e in events if isinstance(e, ReasoningDelta)]
    text = [e for e in events if isinstance(e, TextDelta)]
    assert [e.text for e in reasoning] == ["Let me think..."]
    assert [e.text for e in text] == ["The answer is 4."]


@pytest.mark.asyncio
async def test_reasoning_field_alias_also_yields_reasoning_delta():
    """Some servers use `reasoning` instead of `reasoning_content` for the
    same purpose — both are checked since the field name isn't
    standardized across OpenAI-compatible backends."""
    chunks = [{"choices": [{"delta": {"reasoning": "hmm"}, "finish_reason": None}]}]
    events = [e async for e in parse_sse_stream(_lines(*chunks))]

    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["hmm"]


@pytest.mark.asyncio
async def test_reasoning_only_response_never_yields_text_delta():
    """The exact failure mode this fixes: a model that spends its whole
    response reasoning and never transitions to `content` previously
    vanished completely — now at least the reasoning itself is captured,
    even though there's still no text_delta (there's genuinely no answer)."""
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking a lot "}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "and more"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 500, "total_tokens": 510},
        },
    ]
    events = [e async for e in parse_sse_stream(_lines(*chunks))]

    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == [
        "thinking a lot ",
        "and more",
    ]
    assert not [e for e in events if isinstance(e, TextDelta)]
    assert any(isinstance(e, UsageEvent) and e.usage.completion_tokens == 500 for e in events)
    assert any(isinstance(e, FinishEvent) and e.reason == "stop" for e in events)


@pytest.mark.asyncio
async def test_empty_reasoning_content_is_not_yielded():
    chunks = [{"choices": [{"delta": {"reasoning_content": ""}, "finish_reason": None}]}]
    events = [e async for e in parse_sse_stream(_lines(*chunks))]
    assert not [e for e in events if isinstance(e, ReasoningDelta)]
