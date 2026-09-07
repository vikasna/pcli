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
async def test_chat_stream_sends_max_tokens_when_given():
    import json

    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )

    async with GatewayClient(_settings()) as client:
        await client.collect([ChatMessage(role="user", content="hi")], max_tokens=1234)

    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == 1234


@pytest.mark.asyncio
@respx.mock
async def test_chat_stream_omits_max_tokens_when_not_given():
    import json

    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )

    async with GatewayClient(_settings()) as client:
        await client.collect([ChatMessage(role="user", content="hi")])

    sent = json.loads(route.calls.last.request.content)
    assert "max_tokens" not in sent


@pytest.mark.asyncio
@respx.mock
async def test_chat_stream_sends_temperature_when_given():
    import json

    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )

    async with GatewayClient(_settings()) as client:
        await client.collect([ChatMessage(role="user", content="hi")], temperature=0.2)

    sent = json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0.2


@pytest.mark.asyncio
@respx.mock
async def test_chat_stream_omits_temperature_when_not_given():
    import json

    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )

    async with GatewayClient(_settings()) as client:
        await client.collect([ChatMessage(role="user", content="hi")])

    sent = json.loads(route.calls.last.request.content)
    assert "temperature" not in sent


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


def test_is_configured_does_not_require_api_key():
    settings = _settings(gateway_api_key="")
    assert settings.is_configured()


def test_is_configured_requires_base_url():
    settings = _settings(gateway_base_url="")
    assert not settings.is_configured()


@pytest.mark.asyncio
@respx.mock
async def test_no_auth_header_sent_when_api_key_blank():
    route = respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )

    async with GatewayClient(_settings(gateway_api_key="")) as client:
        await client.collect([ChatMessage(role="user", content="hi")])

    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers}


@pytest.mark.asyncio
@respx.mock
async def test_list_models_parses_openai_style_response():
    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "model-b"}, {"id": "model-a"}], "object": "list"},
        )
    )

    async with GatewayClient(_settings()) as client:
        models = await client.list_models()

    assert models == ["model-a", "model-b"]


@pytest.mark.asyncio
@respx.mock
async def test_list_models_raises_gateway_error_on_failure():
    respx.get("http://fake-gateway.test/v1/models").mock(return_value=httpx.Response(500, text="boom"))

    async with GatewayClient(_settings()) as client:
        with pytest.raises(GatewayError):
            await client.list_models()


@pytest.mark.asyncio
@respx.mock
async def test_detect_context_limit_delegates_to_cost_context_detect():
    """Thin wrapper coverage - the actual multi-backend probing logic is
    covered directly in tests/test_context_detect.py; this just confirms
    GatewayClient wires its own already-authenticated client through to it
    correctly (base_url origin included, real GatewayClient auth headers)."""
    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "fake-model", "context_length": 32768}]})
    )
    respx.get("http://fake-gateway.test/api/v0/models").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/props").mock(return_value=httpx.Response(404))
    respx.post("http://fake-gateway.test/api/show").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/model/info").mock(return_value=httpx.Response(404))

    async with GatewayClient(_settings()) as client:
        assert await client.detect_context_limit("fake-model") == 32768


@pytest.mark.asyncio
@respx.mock
async def test_detect_context_limit_returns_none_when_nothing_matches():
    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "fake-model"}]})
    )
    respx.get("http://fake-gateway.test/api/v0/models").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/props").mock(return_value=httpx.Response(404))
    respx.post("http://fake-gateway.test/api/show").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/model/info").mock(return_value=httpx.Response(404))

    async with GatewayClient(_settings()) as client:
        assert await client.detect_context_limit("fake-model") is None


# --- Actionable error-message hints ---
#
# Regression coverage for a real debugged case: a local model streaming at
# ~5 tokens/sec tripped the 120s request_timeout_s default mid-response, and
# pcli's only trace of it was an opaque "Gateway error: <raw httpx text>"
# with no indication that request_timeout_s was even the relevant knob. The
# hint is folded directly into GatewayError.message (see llm/errors.py), so
# it's checked here via exc_info.value.message/str(exc) — the same thing
# every call site (chat.py, subagent_tool.py, cli.py, ...) already displays.


@pytest.mark.asyncio
@respx.mock
async def test_read_timeout_on_local_api_gateway_hints_at_request_timeout_s():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("the read operation timed out")
    )

    settings = _settings(
        max_retries=1,
        local_api_gateways=["http://fake-gateway.test/v1"],
    )
    assert settings.is_local_api() is True

    async with GatewayClient(settings) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    message = exc_info.value.message
    assert "local API gateway" in message
    assert "request_timeout_s=600" in message  # the local-api floor, not the raw 120s default
    assert "PCLI_REQUEST_TIMEOUT_S" in message


@pytest.mark.asyncio
@respx.mock
async def test_read_timeout_on_hosted_gateway_hints_at_request_timeout_s_without_local_api_framing():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("the read operation timed out")
    )

    async with GatewayClient(_settings(max_retries=1)) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    message = exc_info.value.message
    assert "request_timeout_s=120" in message
    assert "Local models" not in message


@pytest.mark.asyncio
@respx.mock
async def test_connect_timeout_hints_at_checking_the_gateway_is_running():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ConnectTimeout("connect timed out")
    )

    async with GatewayClient(_settings(max_retries=1)) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    message = exc_info.value.message
    assert "Couldn't connect" in message
    assert "http://fake-gateway.test/v1" in message


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_hints_at_checking_the_gateway_is_running():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with GatewayClient(_settings(max_retries=1)) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    assert "Couldn't reach the gateway" in exc_info.value.message


@pytest.mark.asyncio
@respx.mock
async def test_401_response_hints_at_the_configured_api_key():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, content=b'{"error": "unauthorized"}')
    )

    async with GatewayClient(_settings()) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    assert "gateway_api_key" in exc_info.value.message


@pytest.mark.asyncio
@respx.mock
async def test_401_response_without_a_configured_key_hints_at_setting_one():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, content=b'{"error": "unauthorized"}')
    )

    async with GatewayClient(_settings(gateway_api_key="")) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    assert "No gateway_api_key is configured" in exc_info.value.message


@pytest.mark.asyncio
@respx.mock
async def test_429_response_hints_at_max_retries():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(429, content=b"rate limited")
    )

    async with GatewayClient(_settings(max_retries=1)) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    assert "max_retries" in exc_info.value.message


@pytest.mark.asyncio
@respx.mock
async def test_500_response_hints_that_it_is_usually_transient():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(500, content=b"internal error")
    )

    async with GatewayClient(_settings(max_retries=1)) as client:
        with pytest.raises(GatewayError) as exc_info:
            async for _ in client.chat_stream([ChatMessage(role="user", content="hi")]):
                pass

    assert "transient" in exc_info.value.message


@pytest.mark.asyncio
async def test_client_uses_the_local_api_timeout_floor_not_the_raw_setting():
    settings = _settings(
        request_timeout_s=120.0,
        local_api_gateways=["http://fake-gateway.test/v1"],
    )
    async with GatewayClient(settings) as client:
        assert client._client.timeout.read == 600.0


# --- Live/dynamic timeout ---
#
# The timeout used to be baked into the httpx.AsyncClient once at
# construction, so changing Settings.request_timeout_s later (e.g. via a
# /timeout command) had no effect until GatewayClient was rebuilt. Every
# request now passes timeout=self._effective_timeout() explicitly (httpx
# supports a per-request override), reading Settings live each time.


@pytest.mark.asyncio
async def test_effective_timeout_reads_settings_live_not_a_snapshot():
    settings = _settings(request_timeout_s=42.0)
    async with GatewayClient(settings) as client:
        assert client._effective_timeout() == 42.0

        settings.request_timeout_s = 999.0
        assert client._effective_timeout() == 999.0


@pytest.mark.asyncio
@respx.mock
async def test_chat_stream_passes_the_live_effective_timeout_per_request():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )

    settings = _settings(request_timeout_s=42.0)
    captured_timeouts: list[object] = []

    async with GatewayClient(settings) as client:
        original_stream = client._client.stream

        def _spy_stream(*args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            return original_stream(*args, **kwargs)

        client._client.stream = _spy_stream

        await client.collect([ChatMessage(role="user", content="hi")])
        assert captured_timeouts == [42.0]

        # Changed after the first call, with no client rebuild - the very
        # next request must pick it up.
        settings.request_timeout_s = 900.0
        await client.collect([ChatMessage(role="user", content="hi again")])
        assert captured_timeouts == [42.0, 900.0]


@pytest.mark.asyncio
@respx.mock
async def test_list_models_passes_the_live_effective_timeout():
    route = respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a"}]})
    )

    settings = _settings(request_timeout_s=77.0)
    async with GatewayClient(settings) as client:
        original_get = client._client.get
        captured_timeouts: list[object] = []

        def _spy_get(*args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            return original_get(*args, **kwargs)

        client._client.get = _spy_get

        await client.list_models()

    assert route.called
    assert captured_timeouts == [77.0]
