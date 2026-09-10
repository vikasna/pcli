"""Opt-in check: does a real, running model actually support pcli's
tool-calling flow end-to-end?

Every other test in this suite mocks the gateway (respx) so the run is fast
and hermetic. This one is the opposite on purpose — it exercises pcli's real
system prompt, real tool schemas, and real GatewayClient wire format against
whatever OpenAI-compatible server is actually listening, then re-runs the
exact jsonschema.validate step AgentLoop._dispatch_tool_call uses in
production. That's the only way to answer "does this model/chat-template
combination actually work with pcli" — a hand-built curl request can miss a
mismatch in what pcli specifically sends (its full system prompt, its
purpose-field-augmented schemas, ...).

Skipped unless PCLI_LIVE_GATEWAY_URL is set, so it never runs in CI or a
plain `pytest` invocation. Point it at a local gateway to check a model:

    PCLI_LIVE_GATEWAY_URL=http://192.168.68.53:12345/v1 \\
    PCLI_LIVE_GATEWAY_MODEL=llama-3-groq-8b-tool-use \\
    python -m pytest tests/test_live_gateway_tool_calling.py -v -s
"""

from __future__ import annotations

import json
import os

import jsonschema
import pytest

from pcli.agent.prompt import build_system_prompt
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.llm.models import ChatMessage
from pcli.tools.registry import build_default_registry

_GATEWAY_URL = os.environ.get("PCLI_LIVE_GATEWAY_URL")
_MODEL = os.environ.get("PCLI_LIVE_GATEWAY_MODEL")

pytestmark = pytest.mark.skipif(
    not _GATEWAY_URL or not _MODEL,
    reason="set PCLI_LIVE_GATEWAY_URL and PCLI_LIVE_GATEWAY_MODEL to run this against a real gateway",
)


def _settings() -> Settings:
    return Settings(gateway_base_url=_GATEWAY_URL, default_model=_MODEL, request_timeout_s=120.0)


@pytest.mark.asyncio
async def test_model_responds_to_plain_chat():
    """Cheap connectivity/sanity check that runs before the tool-calling
    test below - if this fails, the gateway or model name is the problem,
    not tool-calling specifically."""
    async with GatewayClient(_settings()) as client:
        message, _usage = await client.collect(
            [ChatMessage(role="user", content="Reply with exactly one word: hello")],
            model=_MODEL,
        )
    assert message.content


@pytest.mark.asyncio
async def test_model_calls_a_real_pcli_tool_with_valid_arguments():
    """Sends pcli's actual system prompt plus real tool schemas (as
    AgentLoop would, including the injected `purpose` property) with no
    tool_choice override - pcli never sets one, so this is exactly what
    production sends. A prompt that unambiguously needs a tool call is used
    so that a model/template failing to call anything is a real signal about
    tool-calling support, not prompt ambiguity."""
    registry = build_default_registry().filtered(lambda t: t.name in {"list_dir", "run_shell"})
    tool_defs = registry.to_openai_tools()

    messages = [
        ChatMessage(role="system", content=build_system_prompt()),
        ChatMessage(
            role="user",
            content=(
                "How many files are in the current directory? Use one of the available tools "
                "to find out - don't guess."
            ),
        ),
    ]

    async with GatewayClient(_settings()) as client:
        message, _usage = await client.collect(messages, model=_MODEL, tools=tool_defs)

    assert message.tool_calls, (
        f"Model produced no tool call for an unambiguous tool-requiring prompt. "
        f"content={message.content!r}"
    )

    for call in message.tool_calls:
        tool = registry.get(call.function.name)
        assert tool is not None, f"Model called unknown tool {call.function.name!r}"

        arguments = json.loads(call.function.arguments)
        assert isinstance(arguments, dict)
        # Mirrors AgentLoop._dispatch_tool_call: "purpose" is advertised but
        # never part of the tool's real schema.
        arguments.pop("purpose", None)

        jsonschema.validate(arguments, tool.parameters)
