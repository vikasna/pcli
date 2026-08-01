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
    def from_http_status(cls, status_code: int, message: str) -> GatewayError:
        retryable = status_code == 429 or status_code >= 500
        return cls(message, status_code=status_code, retryable=retryable)

    @classmethod
    def from_network_error(cls, message: str) -> GatewayError:
        return cls(message, status_code=None, retryable=True)
