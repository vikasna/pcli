"""Tracks how much of the model's context window the conversation is using.

There's no local tokenizer for a generic gateway, so this doesn't estimate
from message text — it uses the most recently *reported* usage.total_tokens
(prompt_tokens + completion_tokens of the last actual LLM call). That number
is exactly the size of what gets resent as history on the next call, which
is what "context used" means in practice. Since CostTracker records one
TurnCost per underlying LLM call (not per user-visible turn — a single turn
with tool calls makes several), the *last* entry in Session.cost.turns is
always the most recent call, tool-call round-trips included.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass

from pcli.config.paths import context_limits_file
from pcli.cost.pricing_table import match_model_pattern
from pcli.session.models import Session

DEFAULT_CONTEXT_LIMITS_TOML = """\
# pcli model context-window limits (tokens). Edit freely — entries here
# override pcli's built-in defaults. Model names support a trailing '*' as a
# prefix wildcard, e.g. "gpt-4o*". [default] is used for any model that
# matches nothing else.

[default]
limit = 128000
"""

# Best-effort starting point; intentionally approximate, user-editable.
_BUILTIN_LIMITS: dict[str, int] = {
    "gpt-4o*": 128_000,
    "gpt-4.1*": 1_000_000,
    "gpt-4-turbo*": 128_000,
    "gpt-3.5*": 16_385,
    "o1*": 200_000,
    "o3*": 200_000,
    "claude-opus*": 200_000,
    "claude-sonnet*": 200_000,
    "claude-haiku*": 200_000,
    "gemini-1.5-pro*": 2_000_000,
    "gemini-1.5-flash*": 1_000_000,
    "llama-3*": 128_000,
    "mistral*": 32_000,
}

_DEFAULT_LIMIT = 128_000


class ContextLimitTable:
    def __init__(self, entries: dict[str, int], default: int) -> None:
        self._entries = entries
        self._default = default

    @classmethod
    def load(cls) -> ContextLimitTable:
        entries = dict(_BUILTIN_LIMITS)
        default = _DEFAULT_LIMIT

        path = context_limits_file()
        if path.exists():
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            models = raw.get("models", {})
            for pattern, limit in models.items():
                entries[pattern] = int(limit)
            if "default" in raw and "limit" in raw["default"]:
                default = int(raw["default"]["limit"])

        return cls(entries, default)

    def lookup(self, model_name: str) -> int:
        match = match_model_pattern(model_name, self._entries)
        return match if match is not None else self._default


@dataclass
class ContextUsage:
    used_tokens: int
    limit_tokens: int

    @property
    def fraction(self) -> float:
        return self.used_tokens / self.limit_tokens if self.limit_tokens else 0.0


def current_context_usage(
    session: Session, *, limit_table: ContextLimitTable | None = None
) -> ContextUsage:
    limit_table = limit_table or ContextLimitTable.load()
    used = session.cost.turns[-1].usage.total_tokens if session.cost.turns else 0
    return ContextUsage(used_tokens=used, limit_tokens=limit_table.lookup(session.model))
