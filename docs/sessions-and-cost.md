# Sessions and cost tracking

## Session persistence

`SessionStore` (`src/pcli/session/store.py`) is on-disk CRUD for `Session`
objects (`src/pcli/session/models.py`), rooted at
`sessions_dir()` (`data_dir()/sessions`, `data_dir()` also from
`platformdirs`).

Layout per session:

```
<sessions_dir>/<session_id>/session.json     # the Session, pydantic model_dump_json
<sessions_dir>/<session_id>/blobs/*.txt      # large tool outputs / artifacts
<sessions_dir>/index.json                    # {session_id: SessionIndexEntry} for fast listing
```

All writes are atomic (`_atomic_write`: write to a `.tmp` sibling, then
`os.replace`). `save()` updates both `session.json` and the shared
`index.json` entry (title, updated_at, model, message count, total cost) so
`sessions list` / `/sessions` don't need to load every full session file.

### What's stored on `Session`

- `messages: list[Message]` — full chat history, including tool-call/tool-
  result messages, each with its own id/timestamp.
- `tool_invocations: list[ToolInvocation]` — one record per tool call: name,
  arguments, status (`ok`/`error`/`denied`), a `result_summary` (the possibly-
  truncated preview text), and `full_result_ref` — a blob filename holding
  the untruncated result when one was archived (see "Blob storage" below).
- `cost: SessionCost` — list of `TurnCost` entries plus running totals (see
  Cost tracking below).
- `permission_grants: list[PermissionGrant]` — a model field for recording
  grants against the session. **Note:** nothing in the codebase currently
  appends to it during normal operation — remembered permission grants
  actually live in `PermissionPolicy`'s separate `permissions.json` (see
  [`sandbox-and-permissions.md`](sandbox-and-permissions.md)), not on the
  `Session` object. The field exists and round-trips through export/import,
  but stays empty in practice today.
- `todos: list[TodoItem]` — the `write_todos` tool's task list.
- `metadata: dict` — free-form; used by the importer to stash
  `imported_from_id` / `imported_from_file`.

### Blob storage

Large content — tool results over `artifact_threshold_chars` and the
`fetch_artifact` artifact library — is written to
`<session_dir>/blobs/<name>.txt` via `write_blob`/`write_blob_named` and read
back via `read_blob`. `SessionArtifactStore` (`src/pcli/tools/artifacts.py`)
is the artifact-facing wrapper around this: `put()` generates an `art_...`
id and writes `<id>.txt`; `get()` reads it back (returns `None` if missing).
An `artifact_id` doubles as a `ToolInvocation.full_result_ref` — the chat
screen points a tool-invocation record at the *same* blob AgentLoop already
archived rather than storing the output a second time.

### Export / import

`pcli sessions export <id> [--out PATH] [--gzip]` (`session/export.py`)
bundles a session into a single portable file:

```json
{
  "format": "pcli-session",
  "format_version": 1,
  "exported_at": "...",
  "pcli_version": "0.1.0",
  "session": { ... },
  "blobs": { "<blob_name>": "<content>", ... }
}
```

Only blobs actually referenced via `tool_invocations[*].full_result_ref` are
included. Default filename: `<data_dir>/exports/<session_id>.pcli-session.json`
(or `.pcli-session.json.gz` with `--gzip`, gzip-detected on import via the
magic bytes regardless of extension). The gateway API key is never part of
`Session` in the first place, so there's nothing to redact.

`pcli sessions import <path> [--restore-grants]` (`session/importer.py`):

- Validates the `format`/`format_version` envelope (rejects files from a
  *newer* pcli than the importing one; a `_MIGRATIONS` dict exists for future
  schema migrations, currently empty since there's only ever been version 1).
- **Always assigns a new session id** — the original id is preserved in
  `metadata["imported_from_id"]` — so importing can never collide with or
  overwrite an existing local session.
- **Drops `permission_grants` unless `--restore-grants` is passed** — so an
  imported session can't silently carry "always allow shell"-type grants onto
  a new machine. (In practice this field is normally empty anyway per the
  note above.)
- Restores each referenced blob under its original blob name before saving
  the session.

In the TUI, `/export [path]` exports the *current* session; `/sessions` then
`e` exports the highlighted one from the list screen; `i` there prompts for a
file path and imports it (always with default `restore_grants=False`, no TUI
option to override).

## Cost tracking

### Pricing table

`PricingTable` (`src/pcli/cost/pricing_table.py`) is a small built-in
best-effort USD-per-1M-token table (`_BUILTIN_MODELS`), covering GPT-4o/4.1/
4-turbo/3.5, o1/o3, Claude Opus/Sonnet/Haiku, Gemini 1.5 Pro/Flash, Llama 3,
Mistral — all keyed by a `*`-suffixed prefix pattern (e.g. `"gpt-4o*"`).
Since the gateway is vendor-agnostic there's no single source of truth for
pricing, so this table is meant to be edited: entries in
`config_dir()/pricing.toml` override the built-ins, under a `[models]` table
plus an optional `[default]` table used for any model matching nothing else
(built-in default: `$0`/`$0`).

`match_model_pattern` (shared with `ContextLimitTable`) resolves a model name
to an entry: **exact match wins**; otherwise the **longest matching
`*`-suffixed prefix** pattern.

### Recording cost

`CostTracker.record_turn(model, usage)` (`src/pcli/cost/tracker.py`) is
called once per underlying LLM call (`UsageEvent` from `chat_stream`) — note
this is per *LLM call*, not per user-visible turn: a single turn involving
tool calls makes several. It computes `cost_usd` from the pricing table,
appends a `TurnCost` to `Session.cost.turns`, updates
`session_total_usd`/`total_tokens`, and appends a line to the **global**
ledger at `cost_ledger_file()` (`data_dir()/cost_ledger.jsonl`) — a
newline-delimited JSON log spanning all sessions, independent of any single
session's file.

A subagent's own LLM usage is folded into the *parent* session's cost the
same way, via `ToolResult.extra_usage` (see `spawn_subagent` in
[`tools.md`](tools.md)) — real spend the session total must reflect, even
though the subagent's individual tool calls aren't otherwise recorded.

### `pcli cost report`

`global_cost_report(since=None)` (`cost/tracker.py`) scans the global ledger
and returns `{total_cost_usd, total_tokens, turn_count, by_model}`. `pcli
cost report` prints this as indented JSON. There's currently no CLI flag to
pass `since` — it's only usable programmatically.

### Context-window tracking

`ContextLimitTable` (`src/pcli/cost/context.py`) is the same
pattern/prefix-match table shape as pricing, for token-limit-per-model,
loaded from `config_dir()/context_limits.toml` (`[models]` + `[default]`,
built-in default 128,000 tokens). Built-in limits mirror the pricing table's
model list (e.g. `gpt-4.1*` -> 1,000,000, `claude-*` -> 200,000).

There's no local tokenizer for a generic gateway, so `current_context_usage`
doesn't estimate from message text — it uses the **most recently reported**
`usage.total_tokens` from `Session.cost.turns[-1]` (prompt + completion
tokens of the last actual LLM call), which is exactly the size of what gets
resent as history on the next call. The status bar's `ctx: used/limit (pct%)`
is driven specifically by the *parent* loop's own usage events (never a
subagent's smaller, isolated context) — see `ChatScreen._refresh_context_display`.
