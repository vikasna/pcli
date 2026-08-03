"""Live cost/context/model/sandbox status line."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

# Classic braille "circling" spinner frames.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Ticked only while busy, and well under the ~20fps ceiling that keeps
# repaints smooth on slower (e.g. VTE-based Linux) terminal renderers.
_SPINNER_INTERVAL_S = 0.1


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
    # True for the whole duration the agent is working on a turn (waiting on
    # the LLM, streaming its reply, running tool calls, ...) — cleared only
    # once the response has been fully printed.
    busy: reactive[bool] = reactive(False)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._spinner_index = 0

    def on_mount(self) -> None:
        self.set_interval(_SPINNER_INTERVAL_S, self._advance_spinner)

    def _advance_spinner(self) -> None:
        if not self.busy:
            return
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER_FRAMES)
        self.refresh()

    def render(self) -> str:
        context_part = ""
        if self.context_limit_tokens:
            pct = 100 * self.context_used_tokens / self.context_limit_tokens
            context_part = (
                f"ctx: {format_token_count(self.context_used_tokens)}/"
                f"{format_token_count(self.context_limit_tokens)} ({pct:.0f}%)   "
            )
        spinner_part = f"{_SPINNER_FRAMES[self._spinner_index]} Working...   " if self.busy else ""
        return (
            f"{spinner_part}"
            f"model: {self.model or '-'}   "
            f"cost: ${self.session_cost_usd:.4f}   "
            f"{context_part}"
            f"tokens: {format_token_count(self.total_tokens)}   "
            f"sandbox: {self.sandbox_backend}"
        )
