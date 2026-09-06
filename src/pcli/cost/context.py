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
    def __init__(
        self, entries: dict[str, int], default: int, auto_detected: set[str] | None = None
    ) -> None:
        self._entries = entries
        self._default = default
        # Exact model names (never wildcard patterns — see
        # set_model_context_limit's exact-match docstring) whose [models]
        # entry came from a previous auto-detect run rather than a manual
        # /context-limit correction. Distinguishing the two matters because
        # a local gateway's loaded context (LM Studio, Ollama, ...) can
        # legitimately change between runs (the user reloads the model with
        # a different context-length setting) - so an auto-detected value
        # can go stale in a way a manual override or a hosted API's fixed
        # limit never does. See should_attempt_detection.
        self._auto_detected = auto_detected or set()

    @classmethod
    def load(cls) -> ContextLimitTable:
        entries = dict(_BUILTIN_LIMITS)
        default = _DEFAULT_LIMIT
        auto_detected: set[str] = set()

        path = context_limits_file()
        if path.exists():
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            models = raw.get("models", {})
            for pattern, limit in models.items():
                entries[pattern] = int(limit)
            if "default" in raw and "limit" in raw["default"]:
                default = int(raw["default"]["limit"])
            auto_detected = {name for name, flag in raw.get("auto_detected", {}).items() if flag}

        return cls(entries, default, auto_detected)

    def lookup(self, model_name: str) -> int:
        match = match_model_pattern(model_name, self._entries)
        return match if match is not None else self._default

    def has_explicit_entry(self, model_name: str) -> bool:
        """True if model_name matches a real (builtin or user/auto-set)
        entry, as opposed to lookup() silently falling back to the generic
        default."""
        return match_model_pattern(model_name, self._entries) is not None

    def should_attempt_detection(self, model_name: str) -> bool:
        """Used to gate auto-detection (cost/context_detect.py): True if
        there's no entry for this model at all (the has_explicit_entry==False
        case), OR the entry that's there came from a previous auto-detect
        run rather than a manual /context-limit correction or a built-in
        default. A manual override and a built-in are trusted forever and
        never re-probed; an auto-detected value gets re-checked on every
        startup since it can drift out from under pcli without any pcli-side
        signal (see the class docstring note on _auto_detected)."""
        match = match_model_pattern(model_name, self._entries)
        if match is None:
            return True
        return model_name in self._auto_detected


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


_CEILING_MIN_TOTAL_TOKENS = 2000
_CEILING_STALL_RATIO = 0.1


def looks_like_context_ceiling(session: Session) -> bool:
    """True if, compared to the immediately preceding turn, prompt_tokens
    grew (more history got sent, as it always does turn over turn) but
    total_tokens barely moved — meaning completion_tokens got squeezed down
    to compensate. That's the fingerprint of a real, gateway-enforced
    context ceiling being hit, independent of whatever limit_tokens pcli
    itself assumed via ContextLimitTable (which is silently wrong — falls
    back to a generic 128000-token guess — for any model without a built-in
    or user-configured entry, so the fraction-based auto-compact trigger
    alone can't catch this: a session that's actually 100% full can read as
    a small fraction of an assumed limit that's far too large). Ordinary
    turn-to-turn variation has *both* prompt_tokens and total_tokens growing
    together; only a real ceiling clamps total_tokens while prompt_tokens
    keeps climbing.

    Confirmed against a real debugged session: three consecutive turns each
    landed on total_tokens=16384 (or within a few tokens of it) despite
    prompt_tokens climbing every turn — the model's real ~16k window, for a
    model pcli had no entry for (it was assuming 128000, i.e. reading the
    session as ~13% full when it was actually exhausted)."""
    turns = session.cost.turns
    if len(turns) < 2:
        return False
    current = turns[-1].usage
    previous = turns[-2].usage
    if current.total_tokens < _CEILING_MIN_TOTAL_TOKENS:
        return False
    prompt_growth = current.prompt_tokens - previous.prompt_tokens
    if prompt_growth <= 0:
        return False
    total_growth = current.total_tokens - previous.total_tokens
    return total_growth <= prompt_growth * _CEILING_STALL_RATIO


def set_model_context_limit(model: str, limit: int, *, auto_detected: bool = False) -> None:
    """Persists a per-model context-window override to context_limits.toml's
    [models] table (creating the file if it doesn't exist yet), so a wrong
    built-in guess — or no entry at all — can be corrected without hand-
    editing the file. Exact model-name match, not a wildcard pattern:
    simpler and more predictable for a single correction than guessing a
    sensible glob.

    `auto_detected=True` (the cost/context_detect.py probe path) also
    records the model in the [auto_detected] table, so
    ContextLimitTable.should_attempt_detection knows to re-probe it on a
    later startup rather than trusting it forever. `auto_detected=False`
    (the default — the TUI's /context-limit command) is a human's deliberate
    correction, so it always clears any prior auto-detected marker for this
    model too: once a human has set it, it's sticky forever, even if it was
    auto-detected before."""
    path = context_limits_file()
    raw: dict = {}
    if path.exists():
        raw = dict(tomllib.loads(path.read_text(encoding="utf-8")))

    default_limit = _DEFAULT_LIMIT
    existing_default = raw.get("default")
    if isinstance(existing_default, dict) and "limit" in existing_default:
        default_limit = int(existing_default["limit"])

    models = dict(raw.get("models", {}))
    models[model] = limit

    auto_detected_models = {name for name, flag in raw.get("auto_detected", {}).items() if flag}
    if auto_detected:
        auto_detected_models.add(model)
    else:
        auto_detected_models.discard(model)

    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    lines = ["[default]", f"limit = {default_limit}", "", "[models]"]
    lines.extend(f'"{_escape(pattern)}" = {value}' for pattern, value in models.items())
    if auto_detected_models:
        lines.extend(["", "[auto_detected]"])
        lines.extend(f'"{_escape(name)}" = true' for name in sorted(auto_detected_models))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
