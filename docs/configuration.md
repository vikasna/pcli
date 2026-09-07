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
| `request_timeout_s` | `PCLI_REQUEST_TIMEOUT_S` | *(none)* | `request_timeout_s` | `120.0` | HTTP timeout applied per-request (chat completions, `/models`, health check) via `GatewayClient`'s `_effective_timeout()` helper, which reads `Settings.effective_request_timeout_s` fresh on every call rather than a value baked into the client at construction — so a change takes effect on the very next gateway request, no restart needed. Changeable live in the TUI with [`/timeout`](tui-guide.md#slash-commands), which persists it to `config.toml` the same way `/models <model-id>` persists `default_model`. See the local-api floor below. |
| `max_retries` | `PCLI_MAX_RETRIES` | *(none)* | `max_retries` | `4` | Max attempts for `GatewayClient.chat_stream` on retryable failures (network errors, HTTP 429/5xx) — retried only if no stream data has been yielded yet. |
| `max_tool_iterations` | `PCLI_MAX_TOOL_ITERATIONS` | *(none)* | `max_tool_iterations` | `25` | Cap on tool-call round-trips within a single `AgentLoop.run_turn`; beyond this the loop appends a "reached the max tool-call iteration limit" note and stops. |
| `sandbox_backend` | `PCLI_SANDBOX_BACKEND` | *(none)* | `sandbox_backend` | `"auto"` | `auto` \| `docker` \| `subprocess` \| `none` (see note below). |
| `artifact_threshold_chars` | `PCLI_ARTIFACT_THRESHOLD_CHARS` | `--artifact-threshold` | `artifact_threshold_chars` | `4000` | Tool results longer than this (in characters) are truncated out of the live conversation and archived; see [`tools.md`](tools.md#artifact-archiving). |
| `local_api_gateways` | `PCLI_LOCAL_API_GATEWAYS` | `--local-api` | `local_api_gateways` | `[]` | Gateway base URLs running in local-api mode (uncapped iterations/rate limits, $0 cost). Additive, not a direct override — see [Local-API mode](#local-api-mode) below. |
| `auto_compact_enabled` | `PCLI_AUTO_COMPACT_ENABLED` | *(none)* | `auto_compact_enabled` | `true` | Whether old conversation history is automatically summarized/archived once context usage crosses `auto_compact_threshold`; see [Auto-compaction](#auto-compaction) below. |
| `auto_compact_threshold` | `PCLI_AUTO_COMPACT_THRESHOLD` | *(none)* | `auto_compact_threshold` | `0.8` | Fraction of the model's context limit (`current_context_usage` in `cost/context.py`) at which auto-compaction triggers after a turn completes. |
| `auto_compact_keep_recent_turns` | `PCLI_AUTO_COMPACT_KEEP_RECENT_TURNS` | *(none)* | `auto_compact_keep_recent_turns` | `2` | Number of most-recent user turns left untouched (verbatim) by compaction; only older turns get summarized and archived. |
| `prune_tool_results_enabled` | `PCLI_PRUNE_TOOL_RESULTS_ENABLED` | *(none)* | `prune_tool_results_enabled` | `true` | Whether old tool-call results are automatically shrunk to a compact placeholder to save context, well before auto-compaction's own threshold would trigger; see [Tool-result pruning](#tool-result-pruning) below. |
| `prune_tool_results_keep_recent_turns` | `PCLI_PRUNE_TOOL_RESULTS_KEEP_RECENT_TURNS` | *(none)* | `prune_tool_results_keep_recent_turns` | `1` | Number of most-recent turns whose tool results are left untouched (verbatim); older ones are archived and replaced with a short placeholder. Deliberately tighter than `auto_compact_keep_recent_turns`'s default of `2`. |
| `context_limit_auto_detect_enabled` | `PCLI_CONTEXT_LIMIT_AUTO_DETECT_ENABLED` | *(none)* | `context_limit_auto_detect_enabled` | `true` | Whether pcli tries to query the gateway directly for a model's real context window (see [Automatic context-limit detection](#automatic-context-limit-detection) below) when it has no built-in or user-configured entry for it yet. A handful of extra, short-timeout requests on startup for an unrecognized model; set to `false` to skip this and always fall back to the assumed default (still correctable either way via `/context-limit`). |
| `max_response_tokens_enabled` | `PCLI_MAX_RESPONSE_TOKENS_ENABLED` | *(none)* | `max_response_tokens_enabled` | `true` | Whether pcli sends a dynamic `max_tokens` cap with each request, leaving `max_response_tokens_safety_margin` tokens of headroom below the model's context limit so a single response can't consume the entire remaining window by itself; see [Dynamic response cap](#dynamic-response-cap) below. |
| `max_response_tokens_safety_margin` | `PCLI_MAX_RESPONSE_TOKENS_SAFETY_MARGIN` | *(none)* | `max_response_tokens_safety_margin` | `512` | Tokens of headroom reserved below the model's context limit when computing the dynamic `max_tokens` cap (`context_limit - last_known_used_tokens - this margin`). Ignored when `max_response_tokens_enabled` is `false`. |

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

## Tool-result pruning

`prune_tool_results_enabled` and `prune_tool_results_keep_recent_turns`
(table above) control a second, separate context-saving mechanism,
implemented in `agent/context_pruning.py` and run from
`ChatScreen._run_one_turn` (see [`tui-guide.md`](tui-guide.md#tool-result-pruning)
for the user-facing behavior and the `/prune-tool-results` command, and
[`tools.md`](tools.md#artifact-archiving) for how it reuses the same
archive-then-`fetch_artifact` pattern as artifact archiving).

Unlike [auto-compaction](#auto-compaction) above, this is a purely mechanical
pass with **no LLM call**: it runs every turn, right after the turn is saved
and *before* the auto-compact threshold check, rather than only once context
usage crosses a threshold. It doesn't touch user/assistant messages or
summarize anything — it only replaces the `content` of old tool-role messages
(older than the most recent `prune_tool_results_keep_recent_turns` turns,
default `1`) with a short placeholder, after archiving the original content
via the same `ArtifactStore` mechanism. The default `keep_recent_turns` of
`1` is deliberately tighter than `auto_compact_keep_recent_turns`'s default
of `2`, so pruning routinely has something to do well before compaction's own
threshold would ever be reached. `Message.pruned_artifact_id`
(`src/pcli/session/models.py`) marks an already-pruned message so a later
turn's pass doesn't re-archive (and duplicate) it.

Unlike the three auto-compaction settings above, both pruning settings *are*
persisted via a slash command: [`/prune-tool-results`](tui-guide.md#slash-commands)
writes them to `config.toml` through `update_config_file` and applies them
live on the very next turn, no restart needed.

## Dynamic response cap

`max_response_tokens_enabled` and `max_response_tokens_safety_margin` (table
above) control a third context-safeguard, implemented as
`compute_max_response_tokens` in `cost/context.py` and applied from
`ChatScreen._run_one_turn` (see
[`tui-guide.md`](tui-guide.md#dynamic-response-cap) for the user-facing
behavior and the `/max-response-tokens` command).

It targets a gap the other two mechanisms can't cover: both
[auto-compaction](#auto-compaction) and [tool-result pruning](#tool-result-pruning)
only ever look at context usage *between* turns — after one finishes, before
the next starts. Neither has a checkpoint *inside* a single response while
it's still generating, so a turn that ends comfortably under
`auto_compact_threshold` gives compaction no reason to run, and the very next
turn's own response can then, entirely by itself, consume whatever context
remains and get hard-truncated mid-stream by the gateway's own ceiling. This
is a *complementary*, tighter-grained safeguard for that specific case, not a
replacement for the other two — auto-compaction and pruning still run exactly
as described above.

If `max_response_tokens_enabled` is true (default), pcli computes, fresh
before every turn:

```
max_tokens = context_limit - last_known_used_tokens - max_response_tokens_safety_margin
```

using the same `current_context_usage` basis (`context_limit` from
`ContextLimitTable`, `last_known_used_tokens` from the last recorded
`TurnCost`) that auto-compaction's own threshold check already uses — there's
no local tokenizer to do better (see `cost/context.py`'s module docstring),
so this is exactly as accurate as pcli's other context-usage decisions, no
more, no less. The result is passed as `max_tokens` on the outbound request
via `AgentLoop.set_max_response_tokens` -> `GatewayClient.chat_stream`/
`collect`, applied to every `chat_stream` call a turn makes, including its
own tool-call round-trips (the cap is computed once per user-submitted turn,
not recomputed live as those round-trips add their own usage — see
`AgentLoop.set_max_response_tokens`'s docstring for the reasoning).

This is a **dynamic** cap, not a fixed number: it's recomputed every turn
from whatever `last_known_used_tokens` is at that point, so it shrinks as the
conversation fills up. No `max_tokens` is sent at all (the gateway's own
default applies) in two cases: the first turn of a session, since there's no
prior usage yet to compute headroom from, and once the computed headroom is
already `<= 0` — by that point the window is essentially full and
auto-compaction should already have intervened.

Like the pruning settings, both settings here are persisted via a slash
command: [`/max-response-tokens`](tui-guide.md#slash-commands) writes them to
`config.toml` through `update_config_file` and applies them live starting
with the very next turn.

## Context-limit detection and correction

`ContextLimitTable` (`src/pcli/cost/context.py`) is itself just an assumption
— a small built-in pattern table (`_BUILTIN_LIMITS`,
`src/pcli/cost/context.py:33`) matched against the model name, or a user
override from `context_limits.toml`'s `[models]` table, falling back to a
generic `_DEFAULT_LIMIT` of 128,000 tokens (`src/pcli/cost/context.py:49`)
for any model matching neither. `current_context_usage()`
(`src/pcli/cost/context.py:88`) and the `auto_compact_threshold` trigger
described above are only as accurate as that assumption — for a model with
no matching entry and a real window much smaller than 128,000, the
fraction-based trigger can read a session as mostly empty when the model has
actually run out of room.

`looks_like_context_ceiling(session)` (`src/pcli/cost/context.py:100`) is a
second, independent check for exactly that failure mode: instead of trusting
`ContextLimitTable`'s assumed limit at all, it looks at the session's own
turn-by-turn usage history (`Session.cost.turns`). Ordinary turn-to-turn
growth has both `prompt_tokens` and `total_tokens` climbing together; a real,
gateway-enforced context ceiling instead clamps `total_tokens` (because
`completion_tokens` gets squeezed down to compensate) while `prompt_tokens`
keeps growing as more history gets resent. Comparing the two most recent
turns, if `prompt_tokens` grew but `total_tokens` grew by no more than 10% of
that growth (`_CEILING_STALL_RATIO`) — and the current turn's `total_tokens`
is at least 2,000 (`_CEILING_MIN_TOTAL_TOKENS`, avoiding false positives on
trivial early turns) — that's the fingerprint of a real ceiling. This was
confirmed against a real debugged session with `google/gemma-4-12b-qat` (no
built-in table entry, so pcli was assuming the generic 128,000-token
default): three consecutive turns each landed on `total_tokens` around
16,384 despite `prompt_tokens` climbing every turn — the model's real ~16k
window — while pcli read the session as only ~13% full, so auto-compaction
never fired and the model kept getting cut off with almost no room left to
reply.

`ChatScreen._run_one_turn` (`src/pcli/tui/screens/chat.py:877`) calls this
heuristic whenever a turn ends with no visible reply, and if it fires, the
"model didn't produce a reply" notice (see
[`tui-guide.md`](tui-guide.md#when-a-turn-produces-no-reply-at-all)) names
the currently assumed limit and points at the fix:
[`/context-limit <tokens>`](tui-guide.md#slash-commands). That command calls
`set_model_context_limit(model, limit)` (`src/pcli/cost/context.py:134`),
which persists an exact-model-name entry to `context_limits.toml`'s
`[models]` table — creating the file if it doesn't exist yet, preserving any
other entries already there — the same file manual edits already target, so
hand-editing it and `/context-limit` are two paths to the same effect.
`ChatScreen` reloads its `ContextLimitTable` immediately afterward, so the
corrected limit is used starting with the very next turn — no restart
needed.

### Automatic context-limit detection

Everything above is reactive — pcli only discovers a wrong assumption after
the fact, either from a "no reply" turn or from you noticing and running
`/context-limit` yourself. `detect_context_limit()`
(`src/pcli/cost/context_detect.py`) adds a proactive counterpart: for a
handful of backends, pcli can ask the gateway directly, at startup, what the
model's real context window actually is — closing the generic
128,000-token-fallback gap described above before it ever causes a problem,
for any model those backends recognize.

There's no single standard OpenAI-compatible endpoint for this, so
`detect_context_limit()` tries several backend-specific probes in order
against the gateway's own already-authenticated HTTP client, stopping at the
first one that succeeds:

1. The standard `GET /models` endpoint (the same one `GatewayClient.list_models()`
   already calls) — some backends add an extra field on top of the plain
   OpenAI schema: `context_length` (OpenRouter) or `max_model_len` (vLLM). No
   separate request needed for either.
2. LM Studio's native `GET /api/v0/models` — `max_context_length`, preferring
   `loaded_context_length` when present (the size actually loaded, which can
   differ from the model's max).
3. A raw llama.cpp server's `GET /props` —
   `default_generation_settings.n_ctx`.
4. Ollama's `POST /api/show` (`{"model": "..."}`) — scans `model_info` for
   any key ending in `.context_length` (the architecture-name prefix varies
   per model family, e.g. `llama.context_length`).
5. A LiteLLM proxy's `GET /model/info` — matches the entry by `model_name`,
   then reads `model_info.max_input_tokens`, falling back to
   `model_info.max_tokens` only if that's absent (`max_tokens` is documented
   as unreliable/inconsistently populated across LiteLLM versions, often
   reflecting max *output* tokens instead).

Every probe fails silently (network error, non-2xx, malformed JSON) and
falls through to the next — hosted-only gateways (real OpenAI, Anthropic,
Azure OpenAI, most plain OpenAI-compatible proxies) expose none of this, so
the whole thing just returns `None` there, exactly like an unrecognized
model always has. Detection is also skipped entirely (no network calls at
all) whenever it wouldn't add anything: if `ContextLimitTable.has_explicit_entry(model)`
is already true — which covers pcli's built-in per-model patterns
(`gpt-4o*`, `claude-*`, ...) *and* any earlier manual `/context-limit`
correction or previous successful auto-detection, since both are persisted
to `context_limits.toml` the same way — this never re-probes or silently
overwrites something already known.

**Implementation detail:** four of the five probes above (everything except
the standard `/models` one) hit fixed, well-known paths at the gateway's
*origin* (scheme+host+port), deliberately bypassing whatever path prefix
(typically `/v1`) is baked into the configured `gateway_base_url` —
`_origin_url()` in `context_detect.py` builds each as a fully-qualified
absolute URL rather than a bare path for exactly this reason. That's needed
because `httpx.AsyncClient` does not treat a leading `/` as "reset to origin
root" the way a browser or `urljoin` would — its `_merge_url` always appends
onto `base_url`'s own path regardless of a leading slash, so a bare
`"/props"` against a `base_url` of `http://host:1234/v1` would actually
request `http://host:1234/v1/props`, not the real `http://host:1234/props`
(confirmed against httpx's own source; this was an actual bug caught while
building this feature). Only the first probe (standard `/models`) correctly
inherits that prefix, since it's a real OpenAI-style endpoint that
legitimately lives under it.

This runs once, from `ChatScreen._maybe_detect_context_limit`, called from
`on_mount` right after the `GatewayClient` is constructed, gated by the
`context_limit_auto_detect_enabled` setting above (default `true`). On
success it persists the detected value via the same `set_model_context_limit()`
used by `/context-limit`, reloads the `ContextLimitTable`, updates the status
bar's context-usage display immediately, and shows: `"Auto-detected context
limit for '<model>': <N> tokens."` If every probe fails — including on any
hosted-only gateway, where this is the expected, unavoidable outcome — pcli
shows instead: `"Couldn't auto-detect a context limit for '<model>' — pcli
is assuming <N> tokens. If that's wrong, set it with /context-limit
<tokens>."`, i.e. the same manual correction path documented above remains
the safety net whenever detection can't help. Either way the probe is
wrapped defensively so a failure or timeout never blocks the rest of
startup.

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
   untouched and still fully enforced. Both limits can also be viewed or set
   live from the TUI, without hand-editing `guardrails.toml`, via
   [`/max-tool-calls-per-turn` and
   `/max-tool-calls-per-minute`](tui-guide.md#slash-commands) — though in
   local-api mode a new value only persists for later, since this force-copy
   keeps the running session unlimited regardless.
3. **Cost is forced to $0.** The `CostTracker` is built with a module-level
   `_FREE_PRICING_TABLE` (`PricingTable(entries={}, default=ModelPricing())`)
   instead of the real `PricingTable.load()` — see
   [`sessions-and-cost.md`](sessions-and-cost.md#cost-tracking). Token/context
   tracking is unaffected; only the `$` figure is zeroed.

A fourth effect applies below the `ChatScreen` layer, in `GatewayClient`
itself, so it isn't limited to the TUI: **the effective request timeout is
floored to 600 seconds (10 minutes) for a local-api gateway.**
`Settings.effective_request_timeout_s` — what `GatewayClient` passes as
`timeout=` on each individual request, instead of the raw `request_timeout_s`
field — returns `max(request_timeout_s, 600.0)` when `is_local_api()` is
true, so an explicit `request_timeout_s` higher than 600 still wins, and it's
never *lowered* on your behalf. Non-local-api gateways are unaffected:
`request_timeout_s` (default 120s) is used as-is. This exists because local
model inference (LM Studio, Ollama, ...) is routinely far slower than a
hosted API — no batching, often CPU-bound — and 120s is easily exceeded by
perfectly ordinary local generation; it was added after a real session where
a local model streaming at ~5 tokens/sec tripped the old 120s default
mid-reply and pcli surfaced nothing more useful than a bare "Gateway error."
Because `_effective_timeout()` is re-read on every request rather than fixed
at client construction, this floor (and any manual change to
`request_timeout_s`, e.g. via [`/timeout`](tui-guide.md#slash-commands))
applies starting with the very next gateway request — nothing about it
requires restarting pcli.

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

## Gateway error messages

`GatewayError` (`src/pcli/llm/errors.py`) folds a concrete, actionable hint
into its `.message` whenever the failure maps to something fixable via
configuration, so the hint shows up everywhere the error is surfaced (TUI
turn errors, `/models`, `/compact`, `/toolbox discover`, subagent failures,
and the `pcli toolbox discover` CLI command — see
[`tui-guide.md`](tui-guide.md) for how it's displayed). The most illustrative
case is the one that motivated the timeout floor above: a read timeout
mid-request now names the `effective_request_timeout_s` currently in effect,
suggests a concrete larger value, and says to set it via
`PCLI_REQUEST_TIMEOUT_S` or `config.toml` — calling out that it's the "local
API gateway" and that local models are often much slower than hosted ones,
when applicable. The same pattern (name the setting, suggest a value, say how
to set it) covers connect timeouts, connection errors, HTTP 401/403 (bad or
missing `gateway_api_key`), HTTP 429 (points at `max_retries`), and HTTP 5xx.
The "reached the max tool-call iteration limit" / "reached the guardrail
limit of N tool calls" turn-ending notices (`agent/loop.py`) got the same
treatment: they now name `max_tool_iterations`/`PCLI_MAX_TOOL_ITERATIONS` and
`guardrails.toml`'s `limits.max_tool_calls_per_turn` respectively, and mention
that `--local-api` removes both caps for that gateway. Likewise, an invalid
`sandbox_backend` value now lists the valid options (`auto`, `docker`,
`subprocess`, `none`) instead of just naming the bad one.

## Gateway protocol expectations

`GatewayClient` (`src/pcli/llm/client.py`) speaks the OpenAI chat-completions
wire format: `POST {base_url}/chat/completions` with
`stream_options.include_usage: true` when streaming, and `GET
{base_url}/models` (used by `/models` in the TUI and `health_check`) returning
`{"data": [{"id": "..."}]}`.
