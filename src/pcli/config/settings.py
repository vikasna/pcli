"""Application settings.

Precedence (highest to lowest): explicit constructor kwargs (e.g. CLI flags) >
environment variables > config.toml > built-in defaults.
"""

from __future__ import annotations

import tomllib
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from pcli.config.paths import config_file

# Local model inference (LM Studio, Ollama, ...) is routinely far slower than
# a hosted API - no batching, often CPU-bound - and pcli's own logs have
# caught request_timeout_s's 120s default being exceeded by perfectly normal
# local generation (a model streaming steadily at ~5 tokens/sec can easily
# take several minutes for one reply). Local-api gateways get at least this
# much, regardless of the configured request_timeout_s, unless the user has
# explicitly configured something even higher. See Settings.effective_request_timeout_s.
_LOCAL_API_MIN_TIMEOUT_S = 600.0


class _TomlFileSource(PydanticBaseSettingsSource):
    """Reads config.toml and flattens one level of [table] nesting to table_key."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = config_file()
        if not path.exists():
            return {}
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        flat: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    flat[f"{key}_{sub_key}"] = sub_value
            else:
                flat[key] = value
        return flat


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    gateway_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("PCLI_GATEWAY_URL", "gateway_base_url"),
        description="Base URL of the OpenAI-compatible LLM gateway, e.g. https://gateway.internal/v1",
    )
    gateway_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("PCLI_GATEWAY_API_KEY", "gateway_api_key"),
        repr=False,
    )
    brave_search_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("PCLI_BRAVE_SEARCH_API_KEY", "brave_search_api_key"),
        repr=False,
        description="Optional Brave Search API key for the web_search tool. When unset, "
        "web_search falls back to a best-effort, no-API-key scrape of DuckDuckGo's HTML "
        "results page instead — works out of the box but is inherently more fragile.",
    )
    gateway_auth_header: str = Field(
        default="Authorization",
        validation_alias=AliasChoices("PCLI_GATEWAY_AUTH_HEADER", "gateway_auth_header"),
        description="Header name used to send the API key. Value sent is 'Bearer <key>' for "
        "'Authorization', otherwise the raw key.",
    )
    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("PCLI_TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
        repr=False,
        description="Bot token for `pcli telegram` (from @BotFather). Env-var/config-kwarg "
        "only, same as gateway_api_key - never written by update_config_file, so it's never "
        "persisted to config.toml.",
    )
    telegram_chat_id: int = Field(
        default=0,
        validation_alias=AliasChoices("PCLI_TELEGRAM_CHAT_ID", "telegram_chat_id"),
        description="The one Telegram chat `pcli telegram` will talk to - messages from any "
        "other chat are silently ignored. This is personal automation, not a multi-user bot. "
        "0 means unset.",
    )
    default_model: str = Field(
        default="",
        validation_alias=AliasChoices("PCLI_MODEL", "default_model"),
    )
    default_temperature: float | None = Field(
        default=None,
        validation_alias=AliasChoices("PCLI_DEFAULT_TEMPERATURE", "default_temperature"),
        description="Sampling temperature sent with each request. Unset (default, None) means "
        "no temperature field is sent at all, so the gateway/model's own default applies. "
        "Changeable live with /temperature.",
    )
    request_timeout_s: float = Field(
        default=120.0,
        validation_alias=AliasChoices("PCLI_REQUEST_TIMEOUT_S", "request_timeout_s"),
    )
    max_retries: int = Field(
        default=4,
        validation_alias=AliasChoices("PCLI_MAX_RETRIES", "max_retries"),
    )
    max_tool_iterations: int = Field(
        default=25,
        validation_alias=AliasChoices("PCLI_MAX_TOOL_ITERATIONS", "max_tool_iterations"),
    )
    subagent_max_iterations: int = Field(
        default=30,
        validation_alias=AliasChoices("PCLI_SUBAGENT_MAX_ITERATIONS", "subagent_max_iterations"),
        description="Hard ceiling on a subagent's (spawn_subagent, explore_codebase, etc.) own "
        "tool-call iterations - a model requesting more via spawn_subagent's max_iterations "
        "argument is still capped at this value. Always enforced, even in local-api mode where "
        "max_tool_iterations itself is uncapped: nesting depth/runaway cost is a distinct "
        "safety concern from the parent turn's own iteration limit.",
    )
    sandbox_backend: str = Field(
        default="auto",
        validation_alias=AliasChoices("PCLI_SANDBOX_BACKEND", "sandbox_backend"),
        description="auto | docker | subprocess | none",
    )
    sandbox_cpu_limit_s: int | None = Field(
        default=30,
        validation_alias=AliasChoices("PCLI_SANDBOX_CPU_LIMIT_S", "sandbox_cpu_limit_s"),
        description="RestrictedSubprocessSandbox (the 'subprocess' backend) only, POSIX only: "
        "CPU-time limit (RLIMIT_CPU) applied to every spawned process. None disables it, "
        "leaving the wall-clock timeout as the only cap. No effect on DockerSandbox (which "
        "uses --cpus) or on Windows (no RLIMIT_CPU equivalent).",
    )
    sandbox_memory_limit_bytes: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PCLI_SANDBOX_MEMORY_LIMIT_BYTES", "sandbox_memory_limit_bytes"
        ),
        description="RestrictedSubprocessSandbox (the 'subprocess' backend) only, POSIX only: "
        "virtual-address-space limit (RLIMIT_AS) applied to every spawned process. None "
        "(the default) disables it - RLIMIT_AS bounds virtual memory, not actual usage, and "
        "Go-based CLIs (kubectl, terraform, ...) routinely reserve far more of that than "
        "they actually use, so a default-on limit here made ordinary tool calls fail "
        "outright. Set an explicit byte count only if you deliberately want a memory cap "
        "(e.g. on a constrained VM) and have confirmed your tools tolerate it. No effect on "
        "DockerSandbox (which uses --memory, a real cgroup-enforced limit) or on Windows.",
    )
    artifact_threshold_chars: int = Field(
        default=4000,
        validation_alias=AliasChoices("PCLI_ARTIFACT_THRESHOLD_CHARS", "artifact_threshold_chars"),
        description="Tool results longer than this are truncated out of the live conversation "
        "and archived to the artifact library, retrievable via fetch_artifact.",
    )
    local_api_gateways: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("PCLI_LOCAL_API_GATEWAYS", "local_api_gateways"),
        description="Gateway base URLs running in local-api mode (set via --local-api, paired "
        "to whichever gateway is active at the time): max_tool_iterations and the "
        "guardrails' max_tool_calls_per_turn/per_minute are uncapped, and cost is forced to "
        "$0 rather than looked up in the pricing table (avoids a local model's name "
        "coincidentally matching a paid builtin pricing pattern, e.g. 'llama-3*').",
    )
    auto_compact_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("PCLI_AUTO_COMPACT_ENABLED", "auto_compact_enabled"),
        description="Whether old conversation history is automatically summarized and "
        "archived (see agent/compaction.py) once context usage crosses auto_compact_threshold.",
    )
    auto_compact_threshold: float = Field(
        default=0.8,
        validation_alias=AliasChoices("PCLI_AUTO_COMPACT_THRESHOLD", "auto_compact_threshold"),
        description="Fraction of the model's context limit (see current_context_usage in "
        "cost/context.py) at which auto-compaction triggers after a turn completes.",
    )
    auto_compact_keep_recent_turns: int = Field(
        default=2,
        validation_alias=AliasChoices(
            "PCLI_AUTO_COMPACT_KEEP_RECENT_TURNS", "auto_compact_keep_recent_turns"
        ),
        description="Number of most-recent user turns left untouched (verbatim) by "
        "compaction; only older turns get summarized and archived.",
    )
    memory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("PCLI_MEMORY_ENABLED", "memory_enabled"),
        description="Whether pcli maintains a global, cross-session user-memory profile "
        "(nature of work, preferences, conversation style, recurring task patterns) - "
        "injected into every session's system prompt and extended by the remember tool "
        "(explicit requests) and an automatic extraction pass piggybacked on auto-compaction "
        "(see agent/compaction.py).",
    )
    memory_max_entries: int = Field(
        default=40,
        validation_alias=AliasChoices("PCLI_MEMORY_MAX_ENTRIES", "memory_max_entries"),
        description="Hard cap on the number of stored memory entries - the oldest "
        "source='derived' entry is evicted first once adding a new one would exceed this; "
        "source='explicit' entries (the user directly asked to be remembered) are never "
        "auto-evicted.",
    )
    prune_tool_results_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PCLI_PRUNE_TOOL_RESULTS_ENABLED", "prune_tool_results_enabled"
        ),
        description="Whether old tool-call results are automatically shrunk to a compact "
        "placeholder (see agent/context_pruning.py) to save context, well before "
        "auto-compaction's own threshold would trigger. No LLM call involved, unlike "
        "compaction — a purely mechanical pass run every turn.",
    )
    prune_tool_results_keep_recent_turns: int = Field(
        default=1,
        validation_alias=AliasChoices(
            "PCLI_PRUNE_TOOL_RESULTS_KEEP_RECENT_TURNS", "prune_tool_results_keep_recent_turns"
        ),
        description="Number of most-recent turns whose tool results are left untouched "
        "(verbatim); older ones are archived and replaced with a short placeholder. "
        "Deliberately tighter than auto_compact_keep_recent_turns so pruning actually has "
        "something to do before compaction's threshold is ever reached.",
    )
    context_limit_auto_detect_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PCLI_CONTEXT_LIMIT_AUTO_DETECT_ENABLED", "context_limit_auto_detect_enabled"
        ),
        description="Whether pcli tries to query the gateway directly for a model's real "
        "context window (see cost/context_detect.py) when it has no built-in or "
        "user-configured entry for it yet. A handful of extra, short-timeout requests on "
        "startup for an unrecognized model; set to false to skip this and always fall back "
        "to the assumed default (correctable via /context-limit either way).",
    )
    max_response_tokens_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PCLI_MAX_RESPONSE_TOKENS_ENABLED", "max_response_tokens_enabled"
        ),
        description="Whether pcli sends a dynamic max_tokens cap with each request (see "
        "compute_max_response_tokens in cost/context.py), leaving max_response_tokens_"
        "safety_margin tokens of headroom below the model's context limit so a single "
        "response can't consume the entire remaining window by itself - auto-compaction "
        "only runs between turns and can't stop a runaway response already in progress.",
    )
    max_response_tokens_safety_margin: int = Field(
        default=512,
        validation_alias=AliasChoices(
            "PCLI_MAX_RESPONSE_TOKENS_SAFETY_MARGIN", "max_response_tokens_safety_margin"
        ),
        description="Tokens of headroom reserved below the model's context limit when "
        "computing the dynamic max_tokens cap (context_limit - last_known_usage - this "
        "margin). Ignored when max_response_tokens_enabled is false.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _TomlFileSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    def is_configured(self) -> bool:
        # gateway_api_key is intentionally not required here: local,
        # unauthenticated OpenAI-compatible servers (LM Studio, Ollama, ...)
        # don't need one, and a blank key must not block startup.
        return bool(self.gateway_base_url)

    def is_local_api(self) -> bool:
        return bool(self.gateway_base_url) and self.gateway_base_url in self.local_api_gateways

    def is_telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token) and self.telegram_chat_id != 0

    @property
    def effective_request_timeout_s(self) -> float:
        """The timeout GatewayClient actually applies: request_timeout_s,
        floored to _LOCAL_API_MIN_TIMEOUT_S for local-api gateways (an
        explicit request_timeout_s higher than the floor still wins)."""
        if self.is_local_api():
            return max(self.request_timeout_s, _LOCAL_API_MIN_TIMEOUT_S)
        return self.request_timeout_s


_settings: Settings | None = None


def get_settings(**overrides: Any) -> Settings:
    global _settings
    if overrides or _settings is None:
        _settings = Settings(**overrides)
    return _settings


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump_toml(data: dict[str, Any]) -> str:
    """Minimal TOML serializer for this app's own config.toml: flat scalar
    keys plus at most one level of [table] nesting — the same shape
    _TomlFileSource reads back. Not a general-purpose TOML writer (the
    stdlib has none); good enough since we only ever write our own keys."""
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_toml_scalar(value)}")
    for name, table in tables:
        lines.append("")
        lines.append(f"[{name}]")
        lines.extend(f"{key} = {_toml_scalar(value)}" for key, value in table.items())
    return "\n".join(lines) + "\n"


def update_config_file(**updates: Any) -> None:
    """Persists the given key/value pairs into config.toml, preserving any
    other existing keys/tables. None and "" are skipped rather than written,
    so callers can pass through optional CLI flags/selections unconditionally
    without accidentally clearing a saved preference — but unlike a plain
    truthy check, a real `False`/`0` value (e.g. prune_tool_results_enabled)
    is still written, not silently dropped.

    Used to remember a gateway URL or model picked via a CLI flag or a TUI
    selection (e.g. /models), so a bare `pcli` picks them up next time.
    """
    path = config_file()
    data: dict[str, Any] = {}
    if path.exists():
        data = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    data.update({key: value for key, value in updates.items() if value is not None and value != ""})
    path.write_text(_dump_toml(data), encoding="utf-8")


def remove_config_keys(*keys: str) -> None:
    """Deletes the given top-level keys from config.toml, if present —
    the counterpart to update_config_file for a setting that needs to go
    back to "unset" rather than to some concrete value. update_config_file
    itself can't do this: it deliberately skips a None/"" value instead of
    writing it, so callers can pass optional CLI flags through
    unconditionally without accidentally clearing a saved preference — that
    same skip means it has no way to express "remove this key" (namely
    /temperature off, restoring "no temperature sent" rather than pinning
    it to some specific number)."""
    path = config_file()
    if not path.exists():
        return
    data = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    changed = False
    for key in keys:
        if key in data:
            del data[key]
            changed = True
    if changed:
        path.write_text(_dump_toml(data), encoding="utf-8")


def add_local_api_gateway(gateway_url: str) -> None:
    """Appends `gateway_url` to the persisted `local_api_gateways` list
    (rather than overwriting it, unlike update_config_file) — local-api mode
    is opted into per-gateway, so enabling it for one gateway must not wipe
    out any other gateway already marked local-api."""
    if not gateway_url:
        return
    path = config_file()
    data: dict[str, Any] = {}
    if path.exists():
        data = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    existing = list(data.get("local_api_gateways", []))
    if gateway_url not in existing:
        existing.append(gateway_url)
    data["local_api_gateways"] = existing
    path.write_text(_dump_toml(data), encoding="utf-8")
