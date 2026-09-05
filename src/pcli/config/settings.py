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
    gateway_auth_header: str = Field(
        default="Authorization",
        validation_alias=AliasChoices("PCLI_GATEWAY_AUTH_HEADER", "gateway_auth_header"),
        description="Header name used to send the API key. Value sent is 'Bearer <key>' for "
        "'Authorization', otherwise the raw key.",
    )
    default_model: str = Field(
        default="",
        validation_alias=AliasChoices("PCLI_MODEL", "default_model"),
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
    sandbox_backend: str = Field(
        default="auto",
        validation_alias=AliasChoices("PCLI_SANDBOX_BACKEND", "sandbox_backend"),
        description="auto | docker | subprocess | none",
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
