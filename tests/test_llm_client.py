import httpx
import pytest
import respx

from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.llm.errors import GatewayError
from pcli.llm.models import ChatMessage


def _settings(**overrides) -> Settings:
    defaults = {
        "gateway_base_url": "http://fake-gateway.test/v1",
        "gateway_api_key": "test-key",
        "default_model": "fake-model",
        "max_retries": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _sse(*chunks: dict) -> bytes:
    import json

    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    body += "data: [DONE]\n\n"
    return body.encode()


@pytest.mark.asyncio
@respx.mock
async def test_chat_stream_reconstructs_text_and_usage():
    chunks = [
        {"choices": [{"delta": {"content": "Hello "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    ]
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(*chunks))
    )

    async with GatewayClient(_settings()) as client:
        message, usage = await client.collect([ChatMessage(role="user", content="hi")])

    assert message.content == "Hello world"
    assert usage.total_tokens == 5


@pytest.mark.asyncio
@respx.mock
async def test_chat_stream_reconstructs_fragmented_tool_call():
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "function": {"name": "search_python", "arguments": ""}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"query": '}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '"json"}'}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(*chunks))
    )

    async with GatewayClient(_settings()) as client:
        message, _ = await client.collect([ChatMessage(role="user", content="search for json")])

    assert message.tool_calls is not None
    assert len(message.tool_calls) == 1
    call = message.tool_calls[0]
    assert call.id == "call_1"
    assert call.function.name == "search_python"
    assert call.function.arguments == '{"query": "json"}'


@pytest.mark.asyncio
@respx.mock
async def test_non_retryable_error_raises_immediately():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, content=b'{"error": "unauthorized"}')
    )

    async with GatewayClient(_settings()) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    assert exc_info.value.status_code == 401
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
@respx.mock
async def test_retryable_error_then_success(monkeypatch):
    import pcli.llm.client as client_module

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", _no_sleep)

    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(503, content=b"server busy"),
        httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        ),
    ]

    async with GatewayClient(_settings(max_retries=3)) as client:
        message, _ = await client.collect([ChatMessage(role="user", content="hi")])

    assert message.content == "ok"
    assert route.call_count == 2
