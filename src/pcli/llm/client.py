"""Async client for an OpenAI-compatible chat-completions gateway."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from pcli.config.settings import Settings
from pcli.cost.context_detect import detect_context_limit as _detect_context_limit
from pcli.llm.errors import GatewayError
from pcli.llm.models import ChatMessage, StreamEvent, ToolDefinition, Usage
from pcli.llm.streaming import parse_sse_stream


def _backoff_seconds(attempt: int) -> float:
    return min(20.0, float(2**attempt)) + random.uniform(0, 1)


class GatewayClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.is_configured():
            raise GatewayError("Gateway URL and API key must be configured before use.")
        self._settings = settings
        headers = {"Content-Type": "application/json"}
        if settings.gateway_api_key:
            if settings.gateway_auth_header.lower() == "authorization":
                headers["Authorization"] = f"Bearer {settings.gateway_api_key}"
            else:
                headers[settings.gateway_auth_header] = settings.gateway_api_key
        self._client = httpx.AsyncClient(
            base_url=settings.gateway_base_url.rstrip("/"),
            headers=headers,
            timeout=settings.effective_request_timeout_s,
        )
        # The constructor-level timeout above is only a fallback baseline —
        # every actual request passes timeout=self._effective_timeout()
        # explicitly (httpx supports a per-request override), so a setting
        # change (e.g. via the /timeout command) takes effect on the very
        # next request instead of requiring GatewayClient to be rebuilt.

    def _effective_timeout(self) -> float:
        return self._settings.effective_request_timeout_s

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _network_error_hint(self, exc: httpx.HTTPError) -> str | None:
        """Turns a raw httpx transport failure into something the user can
        actually act on: which setting to change, and to what — grounded in
        a real debugged case where a local model streaming at ~5 tokens/sec
        tripped the 120s default mid-response, and pcli's only trace of it
        was an opaque "Gateway error" with the underlying exception text."""
        timeout = self._settings.effective_request_timeout_s
        suggestion = max(600, int(timeout * 3))
        gateway_kind = "local API gateway" if self._settings.is_local_api() else "gateway"

        if isinstance(exc, httpx.ConnectTimeout):
            return (
                f"Couldn't connect to the {gateway_kind} within {timeout:g}s "
                f"(request_timeout_s={timeout:g}). Check it's actually running at "
                f"{self._settings.gateway_base_url!r} — or, if it's just slow to accept "
                f"connections (e.g. still loading a model), set a larger request_timeout_s "
                f"(e.g. {suggestion}) via PCLI_REQUEST_TIMEOUT_S or config.toml."
            )
        if isinstance(exc, httpx.TimeoutException):
            local_note = (
                "Local models are often much slower than hosted ones — " if self._settings.is_local_api() else ""
            )
            return (
                f"Read timed out on the {gateway_kind} (request_timeout_s={timeout:g}). "
                f"{local_note}Set a larger request_timeout_s (e.g. {suggestion}) via "
                "PCLI_REQUEST_TIMEOUT_S or config.toml."
            )
        if isinstance(exc, httpx.ConnectError):
            return (
                f"Couldn't reach the {gateway_kind} at {self._settings.gateway_base_url!r} — "
                "check that it's running and that gateway_base_url is correct."
            )
        return None

    def _http_status_hint(self, status_code: int, body: str = "") -> str | None:
        # Checked before the status-code-specific branches below: OpenAI-
        # compatible gateways (including llama.cpp/LM Studio) report a
        # context-length overflow as a 400 with wording that varies by
        # backend, so this is a text match on the body rather than a status
        # code alone. Worth surfacing as its own hint rather than a raw
        # gateway error string, since the cause isn't obvious from a bare
        # "invalid_request_error" — this is also the one context-length
        # failure mode pcli's own turn-based auto-compaction can't catch: a
        # subagent's own nested tool-calling loop has no compaction of its
        # own (see tools/_nested_agent.py), so a long subagent task can hit
        # this mid-turn with no prior warning.
        body_lower = body.lower()
        if status_code == 400 and (
            "context_length_exceeded" in body_lower
            or "context length" in body_lower
            or "context window" in body_lower
            or ("maximum" in body_lower and "token" in body_lower)
        ):
            return (
                "This looks like a context-length overflow — the request (conversation history "
                "plus tool results) is larger than the model can accept in one call. If this is "
                "the main conversation, /context-limit sets the context window pcli assumes for "
                "auto-compaction; if it's a subagent's own task, its conversation has no "
                "compaction of its own, so try splitting the task into smaller, narrower steps."
            )
        if status_code in (401, 403):
            if self._settings.gateway_api_key:
                return "The gateway rejected the configured gateway_api_key — check it's correct and hasn't expired."
            return (
                "No gateway_api_key is configured and this gateway appears to require one — "
                "set PCLI_GATEWAY_API_KEY or gateway_api_key in config.toml."
            )
        if status_code == 429:
            return (
                f"Rate-limited by the gateway. pcli already retries with backoff (up to "
                f"max_retries={self._settings.max_retries}) — if this keeps happening, raise "
                "max_retries or reduce request frequency."
            )
        if status_code >= 500:
            return "This is usually transient on the gateway's side — trying again shortly often helps."
        return None

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None,
        tools: list[ToolDefinition] | None,
        stream: bool,
        temperature: float | None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self._settings.default_model,
            "messages": [m.to_wire() for m in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = [t.model_dump() for t in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Streams chat events. Retries (with backoff) only apply to failures that
        happen before any event has been yielded — once partial output has reached
        the caller, retrying the whole request would duplicate it, so a mid-stream
        failure is raised immediately instead.

        `max_tokens`, when given, is the dynamic per-request cap computed by
        cost/context.py's compute_max_response_tokens — see AgentLoop, which
        is what actually sets it.
        """
        payload = self._build_payload(
            messages, model=model, tools=tools, stream=True, temperature=temperature,
            max_tokens=max_tokens,
        )
        max_attempts = max(1, self._settings.max_retries)
        last_error: GatewayError | None = None

        for attempt in range(1, max_attempts + 1):
            started = False
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=payload, timeout=self._effective_timeout()
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        body_text = body.decode(errors="replace")
                        raise GatewayError.from_http_status(
                            response.status_code,
                            body_text,
                            hint=self._http_status_hint(response.status_code, body_text),
                        )
                    async for event in parse_sse_stream(response.aiter_lines()):
                        started = True
                        yield event
                    return
            except GatewayError as exc:
                last_error = exc
                if not exc.retryable or started or attempt == max_attempts:
                    raise
            except httpx.HTTPError as exc:
                last_error = GatewayError.from_network_error(str(exc), hint=self._network_error_hint(exc))
                if started or attempt == max_attempts:
                    raise last_error

            await asyncio.sleep(_backoff_seconds(attempt))

        if last_error:
            raise last_error

    async def collect(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[ChatMessage, Usage]:
        """Consumes chat_stream and returns the final assembled message + usage."""
        from pcli.llm.models import ToolCall, ToolCallCompleteEvent, UsageEvent

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()

        async for event in self.chat_stream(
            messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens
        ):
            if event.kind == "text_delta":
                text_parts.append(event.text)
            elif isinstance(event, ToolCallCompleteEvent):
                tool_calls = event.tool_calls
            elif isinstance(event, UsageEvent):
                usage = event.usage

        message = ChatMessage(
            role="assistant",
            content="".join(text_parts) or None,
            tool_calls=tool_calls or None,
        )
        return message, usage

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def health_check(self) -> bool:
        """Best-effort reachability probe against the gateway's models endpoint."""
        response = await self._client.get("/models", timeout=self._effective_timeout())
        return response.status_code < 500

    async def list_models(self) -> list[str]:
        """Fetches available model IDs from the gateway's OpenAI-compatible
        `GET /models` endpoint (`{"data": [{"id": "..."}, ...]}`)."""
        try:
            response = await self._client.get("/models", timeout=self._effective_timeout())
        except httpx.HTTPError as exc:
            raise GatewayError.from_network_error(str(exc), hint=self._network_error_hint(exc)) from exc
        if response.status_code >= 400:
            raise GatewayError.from_http_status(
                response.status_code,
                response.text,
                hint=self._http_status_hint(response.status_code, response.text),
            )
        payload = response.json()
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [entry["id"] for entry in entries if isinstance(entry, dict) and "id" in entry]
        return sorted(ids)

    async def detect_context_limit(self, model: str) -> int | None:
        """Best-effort: queries this gateway directly for model's real
        context window, trying several known backend-specific extensions
        (see cost/context_detect.py — no single standard covers this).
        None if nothing answered, including for hosted-only gateways that
        expose no such information at all. Never raises."""
        return await _detect_context_limit(self._client, model)
