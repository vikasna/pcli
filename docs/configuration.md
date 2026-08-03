# Configuration

pcli is configured through a `Settings` object (`src/pcli/config/settings.py`,
a `pydantic-settings` `BaseSettings`). Every field can be set via environment
variable or `config.toml`; a subset also has a CLI flag.

## Precedence

Highest to lowest priority (see `Settings.settings_customise_sources`):

1. Explicit constructor kwargs — in practice, the CLI flags `cli.py` passes
   through as overrides.
2. Environment variables.
3. `config.toml`.
4. Built-in defaults (the `Field(default=...)` values below).

## Where config.toml lives

`pcli.config.paths.config_file()` resolves to `<config_dir>/config.toml`,
where `config_dir()` is `platformdirs.PlatformDirs(appname="pcli",
appauthor=False).user_config_dir` — the OS-conventional per-user config
directory (e.g. `%APPDATA%\pcli` on Windows, `~/.config/pcli` on Linux,
`~/Library/Application Support/pcli` on macOS). The directory is created on
first access if missing. Related files live alongside it: `pricing.toml`,
`context_limits.toml`, `guardrails.toml` (all TOML, hand-editable) and
`permissions.json` (JSON, since it's written to at runtime and the stdlib has
no TOML writer). See [`sandbox-and-permissions.md`](sandbox-and-permissions.md)
and [`sessions-and-cost.md`](sessions-and-cost.md) for those.

pcli's own `config.toml` writer (`update_config_file`) only ever emits flat
top-level scalar keys (matching the field names below) plus, if ever needed,
one level of `[table]` nesting — `_TomlFileSource` flattens `[table]` entries
to `table_key` when reading back, so both shapes round-trip.

## Settings fields

| Field | Env var | CLI flag | config.toml key | Default | Purpose |
|---|---|---|---|---|---|
| `gateway_base_url` | `PCLI_GATEWAY_URL` | `--gateway-url` | `gateway_base_url` | `""` | Base URL of the OpenAI-compatible gateway, e.g. `https://gateway.internal/v1`. |
| `gateway_api_key` | `PCLI_GATEWAY_API_KEY` | `--api-key` | `gateway_api_key` | `""` | API key sent to the gateway. Optional — local unauthenticated servers (LM Studio, Ollama) don't need one. |
| `gateway_auth_header` | `PCLI_GATEWAY_AUTH_HEADER` | *(none)* | `gateway_auth_header` | `"Authorization"` | Header used to send the key. If `"Authorization"`, the value sent is `Bearer <key>`; otherwise the raw key is sent under that header name. |
| `default_model` | `PCLI_MODEL` | `--model` | `default_model` | `""` | Model id passed as `model` in chat-completions requests. |
| `request_timeout_s` | `PCLI_REQUEST_TIMEOUT_S` | *(none)* | `request_timeout_s` | `120.0` | HTTP client timeout (`httpx.AsyncClient(timeout=...)`) for gateway requests. |
| `max_retries` | `PCLI_MAX_RETRIES` | *(none)* | `max_retries` | `4` | Max attempts for `GatewayClient.chat_stream` on retryable failures (network errors, HTTP 429/5xx) — retried only if no stream data has been yielded yet. |
| `max_tool_iterations` | `PCLI_MAX_TOOL_ITERATIONS` | *(none)* | `max_tool_iterations` | `25` | Cap on tool-call round-trips within a single `AgentLoop.run_turn`; beyond this the loop appends a "reached the max tool-call iteration limit" note and stops. |
| `sandbox_backend` | `PCLI_SANDBOX_BACKEND` | *(none)* | `sandbox_backend` | `"auto"` | `auto` \| `docker` \| `subprocess` (see note below). |
| `artifact_threshold_chars` | `PCLI_ARTIFACT_THRESHOLD_CHARS` | `--artifact-threshold` | `artifact_threshold_chars` | `4000` | Tool results longer than this (in characters) are truncated out of the live conversation and archived; see [`tools.md`](tools.md#artifact-archiving). |

`Settings.is_configured()` returns `bool(gateway_base_url)` — the API key is
deliberately *not* required, so a blank key never blocks startup against an
unauthenticated local gateway.

**Note on `sandbox_backend`:** the field's docstring says the accepted values
are `auto | docker | subprocess | none`, but `pcli.sandbox.selector.select_sandbox`
only special-cases `"docker"` and `"subprocess"`; any other non-`"auto"`/empty
value — including the documented `"none"` — raises `ValueError: Unknown
sandbox_backend`. There is currently no way to fully disable the sandbox via
this setting.

## CLI flags

Only four settings have a dedicated CLI flag (`src/pcli/cli.py`, root
callback): `--gateway-url`, `--api-key`, `--model`, `--artifact-threshold`.
Everything else must be set via environment variable or `config.toml`. There's
also `--verbose` / `-v`, which raises file logging (`~/.../pcli.log`, see
`configure_logging`) to `DEBUG`; it isn't a `Settings` field.

## Auto-persisted settings

Passing `--gateway-url`, `--model`, and/or `--artifact-threshold` on the
command line writes `gateway_base_url`, `default_model`, and
`artifact_threshold_chars` to `config.toml` (via `update_config_file`), so a
bare `pcli` afterwards picks them up without repeating the flags. Picking a
model with `/models <model-id>` in the TUI does the same for `default_model`.

**The API key is never auto-persisted.** `update_config_file` is deliberately
never called with `gateway_api_key` — pass `--api-key` (or set
`PCLI_GATEWAY_API_KEY`) every time you start pcli if your gateway needs one.

## Gateway protocol expectations

`GatewayClient` (`src/pcli/llm/client.py`) speaks the OpenAI chat-completions
wire format: `POST {base_url}/chat/completions` with
`stream_options.include_usage: true` when streaming, and `GET
{base_url}/models` (used by `/models` in the TUI and `health_check`) returning
`{"data": [{"id": "..."}]}`.
