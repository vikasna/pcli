"""Live cost/context/model/sandbox status line."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class StatusBar(Static):
    model: reactive[str] = reactive("")
    session_cost_usd: reactive[float] = reactive(0.0)
    total_tokens: reactive[int] = reactive(0)
    context_used_tokens: reactive[int] = reactive(0)
    context_limit_tokens: reactive[int] = reactive(0)
    sandbox_backend: reactive[str] = reactive("n/a")

    def render(self) -> str:
        context_part = ""
        if self.context_limit_tokens:
            pct = 100 * self.context_used_tokens / self.context_limit_tokens
            context_part = (
                f"ctx: {format_token_count(self.context_used_tokens)}/"
                f"{format_token_count(self.context_limit_tokens)} ({pct:.0f}%)   "
            )
        return (
            f"model: {self.model or '-'}   "
            f"cost: ${self.session_cost_usd:.4f}   "
            f"{context_part}"
            f"tokens: {format_token_count(self.total_tokens)}   "
            f"sandbox: {self.sandbox_backend}"
        )
