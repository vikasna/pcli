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

### When a session gets saved

In the TUI, `ChatScreen._stream_response` calls `self._store.save(self._session)`:

- **On a successful turn** — after the `async for` loop over `AgentLoop.run_turn`
  finishes, so `session.messages` includes the assistant's response
  (`Message.from_chat_message` for each of `chunk.new_messages` from the
  `turn_complete` event) alongside everything earlier.
- **On a failed turn** (`except GatewayError`) — save also runs here, right
  before the handler returns. This preserves whatever was already appended to
  `session.messages` before the failure (the system prompt, all prior
  successful turns, and the user's own message that triggered this attempt)
  — but *not* an assistant response for the failed turn itself, since
  `run_turn` raises mid-stream and a `turn_complete` event is never yielded to
  extend `session.messages` with one. Before this, a `GatewayError` was never
  saved at all: a turn that failed left no persisted trace, not even the
  triggering user message.

`_run_compaction` also calls `save()` after archiving, so a compaction (auto
or `/compact`) is persisted immediately rather than waiting for the next
turn.

### Pruning empty sessions

`SessionStore.new_session()` calls `save()` **eagerly**, the moment a fresh
session is constructed — this happens before `ChatScreen.__init__` even
appends the leading system-prompt message, and that append is in-memory only
(it isn't re-saved until the first real turn completes or fails, per
"When a session gets saved" above). So every launch of pcli into a brand-new
session writes an index entry immediately, and if the user closes pcli
without ever sending a message, that entry — empty, never touched again —
was left behind permanently, cluttering `index.json` and `/sessions` a
little more with every such launch.

`SessionStore.prune_empty_sessions(*, exclude_session_ids=())` fixes this: it
walks `list_index()` and deletes (via the existing `delete()` method — both
the session directory and its index entry) every entry with
`message_count == 0`, except any id passed in `exclude_session_ids`.
`message_count == 0` is a safe, precise definition of "never had a single
real exchange" because messages in this codebase are only ever appended,
never removed — even a turn that fails outright (the `GatewayError` case
above) persists the system prompt and the user's triggering message first,
so a session that has taken part in any turn at all, successful or not,
never shows a zero count. Returns the number of sessions pruned.

Two call sites run this automatically, each excluding the session it must
not delete out from under itself:

- **`ChatScreen.on_mount`** calls
  `self._store.prune_empty_sessions(exclude_session_ids=[self._session.id])`
  as soon as the chat screen mounts — cheap housekeeping that clears out
  empty sessions left over from *previous* runs every time pcli starts, with
  no user action needed. It excludes the just-started-or-resumed session
  itself, since that session may still legitimately have zero messages at
  this exact point in its lifecycle.
- **`SessionListScreen.on_mount`** (the `/sessions` list) prunes the same
  way before displaying the list, via an optional `exclude_session_id`
  constructor parameter. `ChatScreen`'s `/sessions` command handler passes
  `SessionListScreen(self._store, exclude_session_id=self._session.id)` for
  the same reason — e.g. if the user's very first action after opening pcli
  is typing `/sessions`, the active session is still empty and must not
  vanish while it's the one on screen.

Note that exclusion only suppresses *deletion*: an excluded session with an
existing index entry is still listed in `/sessions` like any other, just
never pruned while it's excluded.

### What's stored on `Session`

- `messages: list[Message]` — full chat history, including tool-call/tool-
  result messages, each with its own id/timestamp.
- `tool_invocations: list[ToolInvocation]` — one record per tool call: name,
  arguments, status (`ok`/`error`/`denied`), a `result_summary` (the possibly-
  truncated preview text), and `full_result_ref` — a blob filename holding
  the untruncated result when one was archived (see "Blob storage" below).
- `cost: SessionCost` — list of `TurnCost` entries plus running totals (see
  Cost tracking below).
- `permission_grants: list[PermissionGrant]` — appended to by
  `PermissionManager.check` whenever a `"session"` or `"always"` grant is
  remembered during this session (see
  [`sandbox-and-permissions.md`](sandbox-and-permissions.md)). This is a
  historical/audit trail that travels with the session, not the enforcement
  store — remembered grants are still enforced via `PermissionPolicy`'s
  separate `permissions.json`, independent of this field.
- `todos: list[TodoItem]` — the `write_todos` tool's task list (replaced
  wholesale on every call).
- `decisions: list[Decision]` — the `record_decision` tool's append-only
  decision log (`decision`, `rationale`, `created_at` per entry); unlike
  `todos`, entries are only ever added, never replaced or mutated — see
  [`tools.md`](tools.md#record_decision).
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

Auto-compaction (`agent/compaction.py`, see [`tools.md`](tools.md#auto-compaction))
reuses this exact field for a different purpose: no real tool call happens,
but `ChatScreen._run_compaction` appends a synthetic `ToolInvocation` with
`tool_name="_compaction"` whose `full_result_ref` points at the archived
transcript blob. This is a deliberate reuse, not an oddity — `export_session`
(below) only bundles blobs it finds referenced via
`tool_invocations[*].full_result_ref`, so piggybacking on that existing field
means a compacted transcript travels with an exported session automatically,
with no new bundling logic needed in `export.py`.

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
  a new machine. Even with `--restore-grants`, this only restores the
  historical record on the `Session` object itself; it deliberately does not
  re-apply those grants into the importing machine's own
  `PermissionPolicy`/`permissions.json`, to avoid double-recording an
  "always" grant that's already persisted separately there.
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

**Local-API mode overrides all of this to $0.** When
`Settings.is_local_api()` is true for the active gateway (`--local-api`, see
[`configuration.md`](configuration.md#local-api-mode)), `ChatScreen` builds
its `CostTracker` with a module-level `_FREE_PRICING_TABLE =
PricingTable(entries={}, default=ModelPricing())` (`src/pcli/tui/screens/
chat.py`) instead of the real `PricingTable.load()` — every model resolves to
the zero `ModelPricing` default regardless of what's in `pricing.toml` or the
built-ins. The motivation is that a local model's name can coincidentally
match a paid builtin prefix pattern above (e.g. a locally-served model named
`llama-3-8b-instruct` or `mistral-7b` would otherwise match `_BUILTIN_MODELS`'
`"llama-3*"` (\$0.20/\$0.20 per 1M) or `"mistral*"` (\$0.25/\$0.75 per 1M) and
show a fake nonzero cost for a free local server). Token/context tracking
(below) is unaffected by local-api mode — only the `$` cost figure is
zeroed.

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
model list (e.g. `gpt-4.1*` -> 1,000,000, `claude-*` -> 200,000). For a model
matching none of those, pcli can also try to fill in the real value
automatically at startup by querying the gateway directly — see
[Automatic context-limit detection](configuration.md#automatic-context-limit-detection)
in `configuration.md` for the probe order and supported backends, and
[Context-limit detection and correction](configuration.md#context-limit-detection-and-correction)
for the manual `/context-limit` fallback.

There's no local tokenizer for a generic gateway, so `current_context_usage`
doesn't estimate from message text — it uses the **most recently reported**
`usage.total_tokens` from `Session.cost.turns[-1]` (prompt + completion
tokens of the last actual LLM call), which is exactly the size of what gets
resent as history on the next call. The status bar's `ctx: used/limit (pct%)`
is driven specifically by the *parent* loop's own usage events (never a
subagent's smaller, isolated context) — see `ChatScreen._refresh_context_display`.
