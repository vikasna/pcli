"""Async client for an OpenAI-compatible chat-completions gateway."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from pcli.config.settings import Settings
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
            timeout=settings.request_timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None,
        tools: list[ToolDefinition] | None,
        stream: bool,
        temperature: float | None,
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
    ) -> AsyncIterator[StreamEvent]:
        """Streams chat events. Retries (with backoff) only apply to failures that
        happen before any event has been yielded — once partial output has reached
        the caller, retrying the whole request would duplicate it, so a mid-stream
        failure is raised immediately instead.
        """
        payload = self._build_payload(
            messages, model=model, tools=tools, stream=True, temperature=temperature
        )
        max_attempts = max(1, self._settings.max_retries)
        last_error: GatewayError | None = None

        for attempt in range(1, max_attempts + 1):
            started = False
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise GatewayError.from_http_status(
                            response.status_code, body.decode(errors="replace")
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
                last_error = GatewayError.from_network_error(str(exc))
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
    ) -> tuple[ChatMessage, Usage]:
        """Consumes chat_stream and returns the final assembled message + usage."""
        from pcli.llm.models import ToolCall, ToolCallCompleteEvent, UsageEvent

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()

        async for event in self.chat_stream(
            messages, model=model, tools=tools, temperature=temperature
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
        response = await self._client.get("/models")
        return response.status_code < 500

    async def list_models(self) -> list[str]:
        """Fetches available model IDs from the gateway's OpenAI-compatible
        `GET /models` endpoint (`{"data": [{"id": "..."}, ...]}`)."""
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise GatewayError.from_network_error(str(exc)) from exc
        if response.status_code >= 400:
            raise GatewayError.from_http_status(response.status_code, response.text)
        payload = response.json()
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [entry["id"] for entry in entries if isinstance(entry, dict) and "id" in entry]
        return sorted(ids)
