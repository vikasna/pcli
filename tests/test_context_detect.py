"""Coverage for cost/context_detect.py: best-effort context-window
detection against several known backend-specific extensions, since there's
no single standard OpenAI-compatible endpoint for this.

The standard-/models probe reuses the client's own base_url (which in
these tests is "http://fake-gateway.test/v1", matching how gateway_base_url
is normally configured) — so it's mocked at ".../v1/models", same as
GatewayClient.list_models() already calls. The other four probes are each
a fixed path at the gateway's *origin*, deliberately bypassing whatever
prefix (here, "/v1") is baked into base_url — confirmed against httpx's own
Client._merge_url, which (unlike a browser or urljoin) always appends onto
base_url's existing path even for a leading-slash "absolute" path, so
reaching a sibling root-level endpoint requires a fully-qualified URL
instead. They're mocked at "http://fake-gateway.test/..." with no "/v1"."""

import httpx
import pytest
import respx

from pcli.cost.context_detect import detect_context_limit

_STANDARD_MODELS_URL = "http://fake-gateway.test/v1/models"
_LMSTUDIO_URL = "http://fake-gateway.test/api/v0/models"
_LLAMA_CPP_PROPS_URL = "http://fake-gateway.test/props"
_OLLAMA_SHOW_URL = "http://fake-gateway.test/api/show"
_LITELLM_MODEL_INFO_URL = "http://fake-gateway.test/model/info"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://fake-gateway.test/v1")


def _mock_all_unused(*, exclude: set[str] = frozenset()) -> None:
    """Registers a 404 for every probe URL not already given a specific
    mock, so respx's strict "all requests must be mocked" mode doesn't
    fail tests that only care about one particular probe succeeding."""
    for url, method in (
        (_STANDARD_MODELS_URL, "GET"),
        (_LMSTUDIO_URL, "GET"),
        (_LLAMA_CPP_PROPS_URL, "GET"),
        (_OLLAMA_SHOW_URL, "POST"),
        (_LITELLM_MODEL_INFO_URL, "GET"),
    ):
        if url in exclude:
            continue
        respx.route(method=method, url=url).mock(return_value=httpx.Response(404))


# --- OpenRouter / vLLM (piggyback on the standard /models response) ---


@pytest.mark.asyncio
@respx.mock
async def test_detects_openrouter_context_length_from_standard_models_endpoint():
    respx.get(_STANDARD_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "my-model", "context_length": 131072}]})
    )
    _mock_all_unused(exclude={_STANDARD_MODELS_URL})
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 131072


@pytest.mark.asyncio
@respx.mock
async def test_detects_vllm_max_model_len_from_standard_models_endpoint():
    respx.get(_STANDARD_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "my-model", "max_model_len": 8192}]})
    )
    _mock_all_unused(exclude={_STANDARD_MODELS_URL})
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 8192


@pytest.mark.asyncio
@respx.mock
async def test_ignores_standard_models_entries_for_a_different_model_id():
    respx.get(_STANDARD_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "other-model", "context_length": 131072}]})
    )
    _mock_all_unused(exclude={_STANDARD_MODELS_URL})
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") is None


# --- LM Studio ---


@pytest.mark.asyncio
@respx.mock
async def test_detects_lmstudio_max_context_length():
    _mock_all_unused()
    respx.get(_LMSTUDIO_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "my-model", "state": "loaded", "max_context_length": 16384}]}
        )
    )
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 16384


@pytest.mark.asyncio
@respx.mock
async def test_lmstudio_prefers_loaded_context_length_over_max():
    _mock_all_unused()
    respx.get(_LMSTUDIO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "my-model", "max_context_length": 262144, "loaded_context_length": 32768}
                ]
            },
        )
    )
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 32768


# --- raw llama.cpp server ---


@pytest.mark.asyncio
@respx.mock
async def test_detects_llama_cpp_props_n_ctx():
    _mock_all_unused()
    respx.get(_LLAMA_CPP_PROPS_URL).mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096}})
    )
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 4096


# --- Ollama ---


@pytest.mark.asyncio
@respx.mock
async def test_detects_ollama_context_length_by_architecture_suffix():
    _mock_all_unused()
    respx.post(_OLLAMA_SHOW_URL).mock(
        return_value=httpx.Response(
            200, json={"model_info": {"gemma4.context_length": 131072, "gemma4.other": 1}}
        )
    )
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 131072


# --- LiteLLM proxy ---


@pytest.mark.asyncio
@respx.mock
async def test_detects_litellm_max_input_tokens():
    _mock_all_unused()
    respx.get(_LITELLM_MODEL_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"model_name": "my-model", "model_info": {"max_tokens": 4096, "max_input_tokens": 128000}}
                ]
            },
        )
    )
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 128000


@pytest.mark.asyncio
@respx.mock
async def test_litellm_falls_back_to_max_tokens_when_max_input_tokens_absent():
    _mock_all_unused()
    respx.get(_LITELLM_MODEL_INFO_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"model_name": "my-model", "model_info": {"max_tokens": 4096}}]}
        )
    )
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 4096


# --- nothing available (hosted-only gateway) ---


@pytest.mark.asyncio
@respx.mock
async def test_returns_none_when_no_backend_exposes_anything():
    respx.get(_STANDARD_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "my-model"}]})
    )
    _mock_all_unused(exclude={_STANDARD_MODELS_URL})
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") is None


@pytest.mark.asyncio
@respx.mock
async def test_returns_none_and_never_raises_on_connection_errors():
    for url, method in (
        (_STANDARD_MODELS_URL, "GET"),
        (_LMSTUDIO_URL, "GET"),
        (_LLAMA_CPP_PROPS_URL, "GET"),
        (_OLLAMA_SHOW_URL, "POST"),
        (_LITELLM_MODEL_INFO_URL, "GET"),
    ):
        respx.route(method=method, url=url).mock(side_effect=httpx.ConnectError("refused"))
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") is None


@pytest.mark.asyncio
@respx.mock
async def test_returns_none_on_malformed_json_response():
    respx.get(_STANDARD_MODELS_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    _mock_all_unused(exclude={_STANDARD_MODELS_URL})
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") is None


@pytest.mark.asyncio
@respx.mock
async def test_probes_are_tried_in_order_first_match_wins():
    """standard /models responds but without a usable field - LM Studio's
    endpoint (tried next) should still be reached and win."""
    respx.get(_STANDARD_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "my-model"}]})
    )
    respx.get(_LMSTUDIO_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "my-model", "max_context_length": 65536}]})
    )
    _mock_all_unused(exclude={_STANDARD_MODELS_URL, _LMSTUDIO_URL})
    async with _client() as client:
        assert await detect_context_limit(client, "my-model") == 65536


def test_origin_url_ignores_the_base_urls_path_prefix():
    from pcli.cost.context_detect import _origin_url

    client = httpx.AsyncClient(base_url="http://fake-gateway.test/v1")
    assert _origin_url(client, "/api/v0/models") == "http://fake-gateway.test/api/v0/models"


def test_origin_url_preserves_a_non_default_port():
    from pcli.cost.context_detect import _origin_url

    client = httpx.AsyncClient(base_url="http://localhost:1234/v1")
    assert _origin_url(client, "/props") == "http://localhost:1234/props"
