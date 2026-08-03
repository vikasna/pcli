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
    other existing keys/tables. Falsy values (None, "") are skipped rather
    than written, so callers can pass through optional CLI flags/selections
    unconditionally without accidentally clearing a saved preference.

    Used to remember a gateway URL or model picked via a CLI flag or a TUI
    selection (e.g. /models), so a bare `pcli` picks them up next time.
    """
    path = config_file()
    data: dict[str, Any] = {}
    if path.exists():
        data = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    data.update({key: value for key, value in updates.items() if value})
    path.write_text(_dump_toml(data), encoding="utf-8")
