"""Per-model USD pricing. The gateway is generic/vendor-agnostic, so pricing
has no single source of truth — it's a small built-in best-effort table,
overridable by the user's pricing.toml."""

from __future__ import annotations

import tomllib
from typing import TypeVar

from pydantic import BaseModel

from pcli.config.paths import pricing_file

T = TypeVar("T")


def match_model_pattern(model_name: str, entries: dict[str, T]) -> T | None:
    """Exact match wins; otherwise the longest '*'-suffixed prefix pattern
    that matches. Shared by PricingTable and context.ContextLimitTable so
    both per-model lookup tables behave identically."""
    if model_name in entries:
        return entries[model_name]
    best_match: T | None = None
    best_len = -1
    for pattern, value in entries.items():
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            if model_name.startswith(prefix) and len(prefix) > best_len:
                best_match = value
                best_len = len(prefix)
    return best_match

DEFAULT_PRICING_TOML = """\
# pcli model pricing (USD per 1M tokens). Edit freely — entries here override
# pcli's built-in defaults. Model names support a trailing '*' as a prefix
# wildcard, e.g. "gpt-4o*". The [default] table is used for any model that
# matches nothing else.

[default]
input_per_1m = 0.0
output_per_1m = 0.0
"""

# Best-effort starting point; intentionally approximate, user-editable.
_BUILTIN_MODELS: dict[str, tuple[float, float]] = {
    "gpt-4o*": (2.50, 10.00),
    "gpt-4.1*": (2.00, 8.00),
    "gpt-4-turbo*": (10.00, 30.00),
    "gpt-3.5*": (0.50, 1.50),
    "o1*": (15.00, 60.00),
    "o3*": (2.00, 8.00),
    "claude-opus*": (15.00, 75.00),
    "claude-sonnet*": (3.00, 15.00),
    "claude-haiku*": (0.80, 4.00),
    "gemini-1.5-pro*": (1.25, 5.00),
    "gemini-1.5-flash*": (0.075, 0.30),
    "llama-3*": (0.20, 0.20),
    "mistral*": (0.25, 0.75),
}


class ModelPricing(BaseModel):
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0


class PricingTable:
    def __init__(self, entries: dict[str, ModelPricing], default: ModelPricing) -> None:
        self._entries = entries
        self._default = default

    @classmethod
    def load(cls) -> PricingTable:
        entries = {
            pattern: ModelPricing(input_per_1m=inp, output_per_1m=out)
            for pattern, (inp, out) in _BUILTIN_MODELS.items()
        }
        default = ModelPricing()

        path = pricing_file()
        if path.exists():
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            models = raw.get("models", {})
            for pattern, values in models.items():
                entries[pattern] = ModelPricing(**values)
            if "default" in raw:
                default = ModelPricing(**raw["default"])

        return cls(entries, default)

    def lookup(self, model_name: str) -> ModelPricing:
        match = match_model_pattern(model_name, self._entries)
        return match if match is not None else self._default

    def cost_usd(self, model_name: str, *, prompt_tokens: int, completion_tokens: int) -> float:
        pricing = self.lookup(model_name)
        return (prompt_tokens / 1_000_000) * pricing.input_per_1m + (
            completion_tokens / 1_000_000
        ) * pricing.output_per_1m
