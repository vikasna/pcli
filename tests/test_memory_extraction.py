"""Coverage for memory/extraction.py: the tool-driven pass that reviews a
compacted-away transcript and calls remember() for anything durable worth
keeping - triggered from chat.py's _run_compaction after a successful
maybe_compact (see test_chat_screen_compaction.py for that end-to-end
wiring). Mirrors test_chat_screen_pruning.py's SSE/tool-call response
helpers, since extraction drives a real tool-calling AgentLoop, unlike
compaction's own single collect() call."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.memory.extraction import extract_memory
from pcli.memory.store import read_memory
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("extraction should never touch the sandbox")


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        max_retries=1,
    )


def _ctx(tmp_path: Path, client: GatewayClient) -> ToolContext:
    return ToolContext(
        sandbox=_NullSandbox(),
        guardrails=GuardrailsConfig(),
        cwd=tmp_path,
        gateway_client=client,
        model="fake-model",
        permission_manager=PermissionManager(),
    )


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str, *, usage: dict | None = None) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return httpx.Response(200, content=_sse(*chunks))


def _remember_call_response(call_id: str, content: str, category: str) -> httpx.Response:
    arguments = json.dumps({"content": content, "category": category})
    return httpx.Response(
        200,
        content=_sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {"name": "remember", "arguments": arguments},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )


@pytest.mark.asyncio
@respx.mock
async def test_extract_memory_persists_what_the_model_chooses_to_remember(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _remember_call_response("call_1", "Works as a backend Python developer", "profile"),
        _text_response("Done reviewing."),
    ]

    async with GatewayClient(_settings()) as client:
        await extract_memory("--- user ---\nI'm a backend Python dev...", _ctx(tmp_path, client))

    entries = read_memory().entries
    assert len(entries) == 1
    assert entries[0].content == "Works as a backend Python developer"
    assert entries[0].category == "profile"
    assert entries[0].source == "derived"


@pytest.mark.asyncio
@respx.mock
async def test_extract_memory_makes_no_calls_when_nothing_is_worth_remembering(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response("Nothing durable here.")
    )

    async with GatewayClient(_settings()) as client:
        await extract_memory("--- user ---\nfix the typo on line 4", _ctx(tmp_path, client))

    assert read_memory().entries == []


@pytest.mark.asyncio
@respx.mock
async def test_extract_memory_can_call_remember_more_than_once(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _remember_call_response("call_1", "Prefers concise commit messages", "style"),
        _remember_call_response("call_2", "Often asks for docs to be updated", "common_ask"),
        _text_response("Done."),
    ]

    async with GatewayClient(_settings()) as client:
        await extract_memory("a long transcript with two durable facts in it", _ctx(tmp_path, client))

    entries = read_memory().entries
    assert {e.content for e in entries} == {
        "Prefers concise commit messages",
        "Often asks for docs to be updated",
    }


@pytest.mark.asyncio
@respx.mock
async def test_extract_memory_returns_usage_for_cost_tracking(tmp_path: Path):
    """chat.py's _extract_memory_from folds this into the session's cost
    ledger (source='memory') - the caller needs real Usage objects back
    whenever the gateway reports them."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response(
            "Nothing durable here.",
            usage={"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
        )
    )

    async with GatewayClient(_settings()) as client:
        usages = await extract_memory("some transcript", _ctx(tmp_path, client))

    assert len(usages) == 1
    assert usages[0].total_tokens == 128
