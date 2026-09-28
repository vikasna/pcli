"""Live cost/context/model/sandbox status line. Grows to a second line only
while a subagent is running (see spawn_subagent) — collapses back to one
line once it finishes."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from pcli.util.text import truncate

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


_MODEL_NAME_MAX_LEN = 40


def format_model_name(model: str) -> str:
    """Shortens an overly long model identifier for the status bar -
    notably a full local file path, which a raw llama-server instance with
    no --alias configured reports as its own "model" id (unlike LM Studio's
    short, friendly names) - e.g. "E:\\...\\Mistral-Nemo-Instruct-2407-
    Q4_K_M.gguf" down to "Mistral-Nemo-Instruct-2407-Q4_K_M". Only the
    display is shortened; the underlying model string used for actual
    requests is untouched. Falls back to plain truncation for anything
    else still too long to fit the line."""
    name = model
    if "/" in name or "\\" in name:
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        if name.lower().endswith(".gguf"):
            name = name[: -len(".gguf")]
    return truncate(name, _MODEL_NAME_MAX_LEN)


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
    # The main agent's own top-level tool-call count for the current turn -
    # reset to 0 at the start of each turn (see ChatScreen._run_one_turn),
    # incremented on each of its own tool_result chunks. Deliberately
    # separate from subagent_tool_calls below: a subagent's internal calls
    # are its own isolated count, not part of this one (spawn_subagent
    # itself counts here as a single call).
    main_tool_calls: reactive[int] = reactive(0)
    # guardrails.max_tool_calls_per_turn as of the start of the current turn
    # (re-read fresh each turn, since it's live-editable via /max-tool-calls
    # -per-turn) - the denominator for the "n/N" display below. 0 means
    # unlimited (same convention the guardrail itself uses, e.g. local-api
    # mode), in which case only "n" is shown - there's no N to divide by.
    main_tool_calls_limit: reactive[int] = reactive(0)
    # Second line, shown only while non-None: a running subagent's progress.
    subagent_task: reactive[str | None] = reactive(None)
    subagent_tool_calls: reactive[int] = reactive(0)
    subagent_last_tool: reactive[str | None] = reactive(None)
    # True while /plan is active (see ChatScreen) — read/explore tools only.
    plan_mode: reactive[bool] = reactive(False)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._spinner_index = 0

    def on_mount(self) -> None:
        self.set_interval(_SPINNER_INTERVAL_S, self._advance_spinner)
        self._sync_height()

    def watch_subagent_task(self, _task: str | None) -> None:
        self._sync_height()

    def _sync_height(self) -> None:
        self.styles.height = 2 if self.subagent_task is not None else 1

    def _advance_spinner(self) -> None:
        if not self.busy:
            return
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER_FRAMES)
        self.refresh()

    def _render_line1(self) -> str:
        context_part = ""
        if self.context_limit_tokens:
            pct = 100 * self.context_used_tokens / self.context_limit_tokens
            context_part = (
                f"ctx: {format_token_count(self.context_used_tokens)}/"
                f"{format_token_count(self.context_limit_tokens)} ({pct:.0f}%)   "
            )
        spinner_part = f"{_SPINNER_FRAMES[self._spinner_index]} Working...   " if self.busy else ""
        plan_part = "[PLAN MODE]   " if self.plan_mode else ""
        tools_part = (
            f"{self.main_tool_calls}/{self.main_tool_calls_limit}"
            if self.main_tool_calls_limit > 0
            else str(self.main_tool_calls)
        )
        return (
            f"{plan_part}"
            f"{spinner_part}"
            f"model: {format_model_name(self.model) if self.model else '-'}   "
            f"cost: ${self.session_cost_usd:.4f}   "
            f"{context_part}"
            f"tokens: {format_token_count(self.total_tokens)}   "
            f"tools: {tools_part}   "
            f"sandbox: {self.sandbox_backend}"
        )

    def render(self) -> str:
        line1 = self._render_line1()
        if self.subagent_task is None:
            return line1
        detail = f", last: {self.subagent_last_tool}" if self.subagent_last_tool else ""
        line2 = (
            f"⟳ Subagent: {truncate(self.subagent_task, 60)} "
            f"— {self.subagent_tool_calls} tool call(s){detail}"
        )
        return f"{line1}\n{line2}"
