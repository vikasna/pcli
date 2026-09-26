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
# matches nothing else. cached_input_per_1m is optional - the rate for
# prompt tokens the gateway reports as served from its prompt cache (see
# llm/models.py's Usage.cached_tokens); omit it if you don't know the
# model's real cache-discount rate, and cached tokens are priced the same
# as ordinary input tokens instead (conservative - never undercounts spend,
# just won't reflect a real discount the gateway might be applying).

[default]
input_per_1m = 0.0
output_per_1m = 0.0
"""

# Best-effort starting point; intentionally approximate, user-editable.
# Third element is cached_input_per_1m (None where no well-established
# cache-discount rate is known for that provider) - OpenAI's documented
# prompt-caching discount is ~50% of input price, Anthropic's cached-read
# rate is ~10% of input price; both approximated here consistently rather
# than guessed per model. Gemini/local models are left at None: real cache
# pricing exists for some of them too but isn't standardized enough here to
# approximate with the same confidence.
_BUILTIN_MODELS: dict[str, tuple[float, float, float | None]] = {
    "gpt-4o*": (2.50, 10.00, 1.25),
    "gpt-4.1*": (2.00, 8.00, 1.00),
    "gpt-4-turbo*": (10.00, 30.00, 5.00),
    "gpt-3.5*": (0.50, 1.50, 0.25),
    "o1*": (15.00, 60.00, 7.50),
    "o3*": (2.00, 8.00, 1.00),
    "claude-opus*": (15.00, 75.00, 1.50),
    "claude-sonnet*": (3.00, 15.00, 0.30),
    "claude-haiku*": (0.80, 4.00, 0.08),
    "gemini-1.5-pro*": (1.25, 5.00, None),
    "gemini-1.5-flash*": (0.075, 0.30, None),
    "llama-3*": (0.20, 0.20, None),
    "mistral*": (0.25, 0.75, None),
}


class ModelPricing(BaseModel):
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0
    cached_input_per_1m: float | None = None
    """Price for prompt tokens the gateway reports as served from its
    prompt cache (Usage.cached_tokens, a subset of prompt_tokens) - None
    (the common case, and the only option for a user-supplied pricing.toml
    entry that doesn't set it) means no cache-specific rate is modeled;
    cost_usd then falls back to pricing input_per_1m for those tokens too,
    same as before this field existed."""


class PricingTable:
    def __init__(self, entries: dict[str, ModelPricing], default: ModelPricing) -> None:
        self._entries = entries
        self._default = default

    @classmethod
    def load(cls) -> PricingTable:
        entries = {
            pattern: ModelPricing(input_per_1m=inp, output_per_1m=out, cached_input_per_1m=cached)
            for pattern, (inp, out, cached) in _BUILTIN_MODELS.items()
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

    def cost_usd(
        self,
        model_name: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
    ) -> float:
        """cached_tokens is the gateway-reported subset of prompt_tokens
        served from its prompt cache (Usage.cached_tokens) - priced at the
        model's cached_input_per_1m rate if known, otherwise at the
        ordinary input rate (no assumed discount). Clamped to prompt_tokens
        so a gateway reporting cached_tokens >= prompt_tokens (not
        guaranteed never to happen across every backend) can't produce a
        negative uncached-token count."""
        pricing = self.lookup(model_name)
        cached_tokens = min(cached_tokens, prompt_tokens)
        uncached_tokens = prompt_tokens - cached_tokens
        cached_rate = (
            pricing.cached_input_per_1m
            if pricing.cached_input_per_1m is not None
            else pricing.input_per_1m
        )
        return (
            (uncached_tokens / 1_000_000) * pricing.input_per_1m
            + (cached_tokens / 1_000_000) * cached_rate
            + (completion_tokens / 1_000_000) * pricing.output_per_1m
        )
