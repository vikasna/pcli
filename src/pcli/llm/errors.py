"""Error hierarchy for the gateway client, with retry classification."""

from __future__ import annotations


class GatewayError(Exception):
    """Raised for any failure talking to the LLM gateway."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    @classmethod
    def from_http_status(cls, status_code: int, message: str, *, hint: str | None = None) -> GatewayError:
        retryable = status_code == 429 or status_code >= 500
        full_message = f"{message} {hint}" if hint else message
        return cls(full_message, status_code=status_code, retryable=retryable)

    @classmethod
    def from_network_error(cls, message: str, *, hint: str | None = None) -> GatewayError:
        # hint is folded directly into .message (not kept as a separate
        # attribute) so every existing call site that already displays
        # str(exc)/exc.message — chat.py's turn/compaction/models handlers,
        # subagent_tool.py's "Subagent failed: {exc}", ... — picks up
        # actionable config guidance automatically, with no per-site change
        # needed. See GatewayClient._network_error_hint/_http_status_hint
        # for what gets suggested and why.
        full_message = f"{message} {hint}" if hint else message
        return cls(full_message, status_code=None, retryable=True)
