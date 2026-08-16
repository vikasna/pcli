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
| `sandbox_backend` | `PCLI_SANDBOX_BACKEND` | *(none)* | `sandbox_backend` | `"auto"` | `auto` \| `docker` \| `subprocess` \| `none` (see note below). |
| `artifact_threshold_chars` | `PCLI_ARTIFACT_THRESHOLD_CHARS` | `--artifact-threshold` | `artifact_threshold_chars` | `4000` | Tool results longer than this (in characters) are truncated out of the live conversation and archived; see [`tools.md`](tools.md#artifact-archiving). |
| `local_api_gateways` | `PCLI_LOCAL_API_GATEWAYS` | `--local-api` | `local_api_gateways` | `[]` | Gateway base URLs running in local-api mode (uncapped iterations/rate limits, $0 cost). Additive, not a direct override — see [Local-API mode](#local-api-mode) below. |
| `auto_compact_enabled` | `PCLI_AUTO_COMPACT_ENABLED` | *(none)* | `auto_compact_enabled` | `true` | Whether old conversation history is automatically summarized/archived once context usage crosses `auto_compact_threshold`; see [Auto-compaction](#auto-compaction) below. |
| `auto_compact_threshold` | `PCLI_AUTO_COMPACT_THRESHOLD` | *(none)* | `auto_compact_threshold` | `0.8` | Fraction of the model's context limit (`current_context_usage` in `cost/context.py`) at which auto-compaction triggers after a turn completes. |
| `auto_compact_keep_recent_turns` | `PCLI_AUTO_COMPACT_KEEP_RECENT_TURNS` | *(none)* | `auto_compact_keep_recent_turns` | `2` | Number of most-recent user turns left untouched (verbatim) by compaction; only older turns get summarized and archived. |

`Settings.is_configured()` returns `bool(gateway_base_url)` — the API key is
deliberately *not* required, so a blank key never blocks startup against an
unauthenticated local gateway.

**Note on `sandbox_backend`:** `"none"` selects `NullSandbox`
(`src/pcli/sandbox/null_backend.py`) — it runs commands directly, with none of
`RestrictedSubprocessSandbox`'s containment (no cwd jail, no env scrubbing, no
resource limits) or Docker's isolation. This is an explicit, documented
opt-out for environments you already trust fully (e.g. pcli running inside
its own disposable container/VM) — it is never the default and should not be
used against an untrusted project. Guardrails and the permission system still
gate every tool call as usual above it; `"none"` only removes the *execution*
containment layer underneath them. See
[`sandbox-and-permissions.md`](sandbox-and-permissions.md#nullsandbox) for
details.

## CLI flags

Five settings-related flags exist (`src/pcli/cli.py`, root callback):
`--gateway-url`, `--api-key`, `--model`, `--artifact-threshold`, and
`--local-api`. Everything else must be set via environment variable or
`config.toml`. There's also `--verbose` / `-v`, which raises file logging
(`~/.../pcli.log`, see `configure_logging`) to `DEBUG`; it isn't a `Settings`
field. Historically `pcli.log` only ever captured `httpx`'s own
request/response logging (nothing from pcli's own code called into `logging`
at all); `ChatScreen._stream_response` now also logs a full traceback there
(`logger.exception(...)`, `ERROR` level, so it's captured regardless of
`--verbose`) whenever a turn fails with a `GatewayError`.

`--local-api` is different in kind from the other four: it's a boolean flag
that doesn't set a scalar override, it *appends* the active gateway to a
persisted list (see [Local-API mode](#local-api-mode) below).

## Auto-persisted settings

Passing `--gateway-url`, `--model`, and/or `--artifact-threshold` on the
command line writes `gateway_base_url`, `default_model`, and
`artifact_threshold_chars` to `config.toml` (via `update_config_file`), so a
bare `pcli` afterwards picks them up without repeating the flags. Picking a
model with `/models <model-id>` in the TUI does the same for `default_model`.

**The API key is never auto-persisted.** `update_config_file` is deliberately
never called with `gateway_api_key` — pass `--api-key` (or set
`PCLI_GATEWAY_API_KEY`) every time you start pcli if your gateway needs one.

## Auto-compaction

`auto_compact_enabled`, `auto_compact_threshold`, and
`auto_compact_keep_recent_turns` (table above) control automatic
summarization of old conversation history, implemented in
`agent/compaction.py` and triggered from `ChatScreen._stream_response` (see
[`tools.md`](tools.md#auto-compaction) for the mechanism and
[`tui-guide.md`](tui-guide.md) for the manual `/compact` command).
**None of these three has a CLI flag, and none is auto-persisted to
`config.toml`** — every other setting in the table above has at least one of
those; these three are env-var/config.toml-only.

The design is necessarily reactive: pcli has no local tokenizer to predict
token usage before a request is sent (see `cost/context.py`'s own
docstring), so it can only check `current_context_usage(...).fraction`
*after* a turn finishes and, if warranted, compact *before* the next one
starts — there's no way to pre-empt a request that's about to blow the
context budget. The check runs once per turn, right after the session is
saved: if `auto_compact_enabled` is true and the fraction is `>=
auto_compact_threshold` (default 0.8, i.e. 80% of the model's context
limit), compaction runs automatically. It always leaves the
`auto_compact_keep_recent_turns` most-recent user turns (default 2)
untouched and summarizes everything older in one dedicated LLM call.
Recompaction needs no special-casing: a later compaction naturally includes
a prior compaction's own summary message among the older messages it folds
into a fresh combined summary.

## Local-API mode

`--local-api` doesn't fit the "overwrite this key" model above: passing it
calls `add_local_api_gateway(settings.gateway_base_url)`
(`src/pcli/config/settings.py`), which *appends* the currently active
`gateway_base_url` to the persisted `local_api_gateways` array in
`config.toml` rather than replacing it — unlike `update_config_file`, which
overwrites whichever keys it's given. `Settings.is_local_api()` is just
`gateway_base_url in local_api_gateways`, so the mode is paired to that one
gateway URL, not a global switch: point pcli at a different, non-local-api
gateway and normal limits/pricing apply again automatically. There's no
`--no-local-api` flag — un-marking a gateway means hand-editing
`local_api_gateways` out of `config.toml` yourself. If `--local-api` is
passed with no gateway configured (no `--gateway-url` and none
persisted/env-set), `cli.py` prints a warning to stderr and does nothing
(doesn't crash).

When `is_local_api()` is true for the active gateway, `ChatScreen.__init__`
(`src/pcli/tui/screens/chat.py`) changes three things for that session, all
verified in-code rather than toggled via `Settings` fields:

1. **`max_tool_iterations` becomes unlimited.** `AgentLoop`'s per-turn
   LLM<->tool round-trip cap (default 25, see the settings table above)
   becomes `None` via `ChatScreen._effective_max_tool_iterations()`.
2. **The guardrails' turn/rate limits become unlimited.**
   `max_tool_calls_per_turn` and `max_tool_calls_per_minute` (defaults 25 and
   60, see [`sandbox-and-permissions.md`](sandbox-and-permissions.md)) are
   both forced to `0` on a `.model_copy()` of the loaded `GuardrailsConfig`
   used for that screen's `PermissionManager` — a `0` there already means
   "unlimited" in both enforcement points. The *security* guardrails (shell
   denylist, fs `allowed_roots`/`deny_paths`, python `module_denylist`) are
   untouched and still fully enforced.
3. **Cost is forced to $0.** The `CostTracker` is built with a module-level
   `_FREE_PRICING_TABLE` (`PricingTable(entries={}, default=ModelPricing())`)
   instead of the real `PricingTable.load()` — see
   [`sessions-and-cost.md`](sessions-and-cost.md#cost-tracking). Token/context
   tracking is unaffected; only the `$` figure is zeroed.

A subagent spawned via `spawn_subagent` keeps its own separate iteration cap
(`_DEFAULT_MAX_ITERATIONS = 15`, `src/pcli/tools/builtin/subagent_tool.py`)
regardless of local-api mode — nesting depth/runaway recursion is treated as
a distinct, deliberately non-configurable structural safety cap, not the
same concern as turn-count or cost limiting.

`ChatScreen` also shows a one-time system message on mount when local-api
mode is active for the session's gateway.

The motivation is local/free OpenAI-compatible servers (LM Studio, Ollama,
...): there's no real turn-count or cost concern against them, so the normal
safety-oriented limits (sized for paid, rate-limited gateways) are just
friction.

## Gateway protocol expectations

`GatewayClient` (`src/pcli/llm/client.py`) speaks the OpenAI chat-completions
wire format: `POST {base_url}/chat/completions` with
`stream_options.include_usage: true` when streaming, and `GET
{base_url}/models` (used by `/models` in the TUI and `health_check`) returning
`{"data": [{"id": "..."}]}`.
