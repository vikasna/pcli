"""Best-effort auto-detection of a model's real context window, queried
directly from the gateway.

There's no single standard OpenAI-compatible endpoint for this — the vanilla
`GET /models` response (per the OpenAI spec) carries only `id`/`object`/
`created`/`owned_by`, nothing about context size. Every backend that DOES
expose it does so through its own extension:

- OpenRouter and vLLM both add an extra field (`context_length` /
  `max_model_len` respectively) directly onto the standard `/models`
  response — covered for free by the first probe below, no separate
  endpoint needed.
- LM Studio has its own native `GET /api/v0/models` endpoint
  (`max_context_length`, and possibly `loaded_context_length` for a
  currently-loaded model).
- A raw llama.cpp server (not behind LM Studio) exposes `GET /props`,
  whose `default_generation_settings.n_ctx` reflects whatever single model
  it has loaded.
- Ollama exposes `POST /api/show` (`{"model": "..."}`), whose `model_info`
  has a `<arch>.context_length` key (the architecture name varies per
  model family, hence the suffix scan below rather than a fixed key).
- A LiteLLM proxy exposes `GET /model/info`, returning every configured
  model's `model_name` alongside a `model_info` object with
  `max_input_tokens` (the real context window — `max_tokens` on the same
  object is unreliable/inconsistently populated across LiteLLM versions,
  often reflecting max *output* tokens instead, so it's only used as a
  fallback when `max_input_tokens` is absent).

Hosted-only gateways (real OpenAI, Anthropic, Azure OpenAI, most plain
OpenAI-compatible proxies) expose none of this — every probe here just
fails cleanly and detect_context_limit() returns None, exactly as if the
model were simply unrecognized.

The standard-/models probe deliberately reuses `client`'s own configured
base_url as-is (same path GatewayClient.list_models() already calls),
since it's a real OpenAI-style endpoint that legitimately lives under
whatever prefix (typically '/v1', sometimes something custom) the user
configured. The other four are each a fixed, well-known path at the
gateway's *origin* (scheme+host+port) — they must NOT inherit that prefix,
so `_origin_url` builds each one as a fully-qualified absolute URL rather
than a bare path. This matters because httpx.AsyncClient deliberately does
NOT treat a leading '/' as "reset to origin root" the way browsers/urljoin
do — Client._merge_url always appends onto base_url's own path regardless
of a leading slash (confirmed directly against httpx's source: it exists
specifically so a relative-looking path composes predictably under a
subpath base_url) — only a fully-qualified URL (with its own scheme)
bypasses that merging entirely.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

_PROBE_TIMEOUT_S = 5.0


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _origin_url(client: httpx.AsyncClient, path: str) -> str:
    """A fully-qualified URL at client's origin (scheme+host+port),
    ignoring whatever path (e.g. '/v1') is baked into its base_url — see
    the module docstring for why this can't just be a plain '/path'."""
    base = client.base_url
    port_part = f":{base.port}" if base.port else ""
    return f"{base.scheme}://{base.host}{port_part}{path}"


async def _probe_standard_models_endpoint(
    client: httpx.AsyncClient, model: str, timeout: float
) -> int | None:
    response = await client.get("/models", timeout=timeout)
    if response.status_code >= 400:
        return None
    entries = response.json().get("data", [])
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") != model:
            continue
        for key in ("context_length", "max_model_len"):
            limit = _positive_int(entry.get(key))
            if limit is not None:
                return limit
    return None


async def _probe_lmstudio(client: httpx.AsyncClient, model: str, timeout: float) -> int | None:
    response = await client.get(_origin_url(client, "/api/v0/models"), timeout=timeout)
    if response.status_code >= 400:
        return None
    entries = response.json().get("data", [])
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") != model:
            continue
        # loaded_context_length only appears once LM Studio has actually
        # loaded the model into memory. LM Studio JIT-loads on first
        # inference and unloads after an idle TTL, so at pcli's startup-time
        # probe the model is very often still "not-loaded" - the entry then
        # has only max_context_length, the architecture's *maximum
        # supported* window, which can be many times larger than what
        # actually gets allocated once loaded (confirmed against a real
        # session: max_context_length=262144 for a model llama.cpp had
        # loaded with n_ctx_slot=16384). Trusting that fallback previously
        # reintroduced the exact stale-limit bug should_attempt_detection
        # was built to fix, just from a different angle - a "don't know"
        # (None, falls through to the next probe / the generic default)
        # is safer than a confidently wrong number here.
        return _positive_int(entry.get("loaded_context_length"))
    return None


async def _probe_llama_cpp_props(client: httpx.AsyncClient, model: str, timeout: float) -> int | None:
    response = await client.get(_origin_url(client, "/props"), timeout=timeout)
    if response.status_code >= 400:
        return None
    settings = response.json().get("default_generation_settings", {})
    return _positive_int(settings.get("n_ctx")) if isinstance(settings, dict) else None


async def _probe_ollama(client: httpx.AsyncClient, model: str, timeout: float) -> int | None:
    response = await client.post(
        _origin_url(client, "/api/show"), json={"model": model}, timeout=timeout
    )
    if response.status_code >= 400:
        return None
    model_info = response.json().get("model_info", {})
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if key.endswith(".context_length"):
            limit = _positive_int(value)
            if limit is not None:
                return limit
    return None


async def _probe_litellm(client: httpx.AsyncClient, model: str, timeout: float) -> int | None:
    response = await client.get(_origin_url(client, "/model/info"), timeout=timeout)
    if response.status_code >= 400:
        return None
    entries = response.json().get("data", [])
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("model_name") != model:
            continue
        model_info = entry.get("model_info")
        if not isinstance(model_info, dict):
            continue
        for key in ("max_input_tokens", "max_tokens"):
            limit = _positive_int(model_info.get(key))
            if limit is not None:
                return limit
    return None


_PROBES: tuple[Callable[[httpx.AsyncClient, str, float], Awaitable[int | None]], ...] = (
    _probe_standard_models_endpoint,
    _probe_lmstudio,
    _probe_llama_cpp_props,
    _probe_ollama,
    _probe_litellm,
)


async def detect_context_limit(
    client: httpx.AsyncClient, model: str, *, timeout: float = _PROBE_TIMEOUT_S
) -> int | None:
    """Tries each known backend-specific probe in turn against `client`'s
    already-configured base_url/auth, returning the first positive context
    size found. Every probe swallows its own connection/parse failures
    (a backend that doesn't support it looks exactly like one that isn't
    reachable — both just mean "try the next one" or, if all fail, "give
    up and let the caller fall back to the assumed/manual limit"), so this
    never raises."""
    for probe in _PROBES:
        try:
            limit = await probe(client, model, timeout)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            continue
        if limit is not None:
            return limit
    return None
