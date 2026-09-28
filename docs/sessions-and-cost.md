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
- `working_dir: str | None` — `Path.cwd()` at session creation
  (`ChatScreen.__init__`, for a brand-new session only). Resuming a session
  — via `/sessions` or `pcli --resume <id>` — compares this against the
  current directory (`session/directory_check.py`'s `directory_mismatch()`)
  and warns, but doesn't block, if they differ: relative paths in the
  session's history, and any new tool calls, would otherwise resolve against
  the wrong directory. `None` for sessions predating this field, or ones
  never tied to a real cwd (test fixtures, imported sessions — see
  "Export / import" below) — never treated as a mismatch.

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
- **Always clears `working_dir`** — it names a directory on the exporting
  machine, essentially never the importing one, so keeping it would just
  trip the directory-mismatch warning (see "What's stored on `Session`"
  above) on every resume of the imported session for no reason.
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

Each entry (`ModelPricing`) also has an optional `cached_input_per_1m: float
| None = None` — the rate for prompt tokens the gateway reports as served
from its own prompt cache (`Usage.cached_tokens`, parsed in
`llm/streaming.py` off an OpenAI-compatible gateway's
`prompt_tokens_details.cached_tokens` field). `None` (the default, and what
a `pricing.toml` entry gets if it doesn't set the key) means no
cache-specific rate is modeled: cached tokens are then priced the same as
ordinary input tokens, i.e. no assumed discount — this never *undercounts*
real spend, it just won't reflect an actual discount the gateway might be
applying. The built-ins approximate `cached_input_per_1m` for the OpenAI and
Anthropic families already in the table, using well-established
industry-wide discount ratios: ~50% of input price for OpenAI models
(gpt-4o, gpt-4.1, gpt-4-turbo, gpt-3.5, o1, o3), ~10% of input price for
Anthropic models (claude-opus, claude-sonnet, claude-haiku). The Gemini/
Llama/Mistral entries are left at `None` — no confident industry-standard
ratio applied there. `PricingTable.cost_usd(model_name, *, prompt_tokens,
completion_tokens, cached_tokens=0)` splits the calculation accordingly:
`cached_tokens` (clamped to `prompt_tokens`, in case a gateway ever reports
`cached_tokens >= prompt_tokens`) is priced at the cached rate (or the
ordinary input rate, if unknown for that model), and the remaining
uncached prompt tokens at the full input rate as before.
`CostTracker.record_turn` passes `cached_tokens=usage.cached_tokens or 0`
automatically, so no caller needs to change to benefit from this — before
this, `Usage.cached_tokens` was already parsed off the wire but never
consulted anywhere, so pcli's own cost display silently overstated real
spend on any cache-aware gateway.

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

`CostTracker.record_turn(model, usage, *, source="main")` (`src/pcli/cost/
tracker.py`) is called once per underlying LLM call (`UsageEvent` from
`chat_stream`) — note this is per *LLM call*, not per user-visible turn: a
single turn involving tool calls makes several. It computes `cost_usd` from
the pricing table, appends a `TurnCost` to `Session.cost.turns`, updates
`session_total_usd`/`total_tokens`, and appends a line to the **global**
ledger at `cost_ledger_file()` (`data_dir()/cost_ledger.jsonl`) — a
newline-delimited JSON log spanning all sessions, independent of any single
session's file. Each ledger line now also carries a `cached_tokens` field
(the same value threaded into `cost_usd` above, `0` when the gateway
reported none) alongside `prompt_tokens`/`completion_tokens`/`total_tokens`
— previously absent, added for transparency into how much of a call's cost
reflects cache-priced tokens.

A subagent's own LLM usage is folded into the *parent* session's cost the
same way, via `ToolResult.extra_usage` (see `spawn_subagent` in
[`tools.md`](tools.md)) — real spend the session total must reflect, even
though the subagent's individual tool calls aren't otherwise recorded. The
same field also covers a tool that makes its own side LLM call directly,
with no nested `AgentLoop` involved at all — `ask_artifact`
([`tools.md`](tools.md#ask_artifact)) is the current example, reporting the
`Usage` from its single `ctx.gateway_client.collect()` call the same way.

#### `TurnCost.source`

Every `TurnCost` (`src/pcli/session/models.py`) carries a `source: Literal["main",
"subagent", "compaction", "memory"]` field, defaulting to `"main"` (so a
session persisted before this field existed still validates and behaves
exactly as it did before — every entry back then was implicitly a main-turn
entry). `ChatScreen` (`src/pcli/tui/screens/chat.py`) tags each of its four
real `record_turn(...)` call sites explicitly:

- **`"main"`** (the default, passed implicitly) — the plain per-turn `usage`
  chunk handler, i.e. an actual LLM call that's part of the main
  conversation the user is having.
- **`"compaction"`** — `_run_compaction`'s own summarization call, whether
  triggered automatically (crossing `auto_compact_threshold`) or via
  `/compact`. Tagged with whatever model the call actually used —
  `ChatScreen._effective_compaction_model()` (`settings.compaction_model`
  when set, see [`configuration.md`](configuration.md#settings-fields),
  else `default_model`/`session.model`) — not hardcoded to `default_model`,
  so this cost tag can't mismatch the model actually billed.
- **`"subagent"`** — a tool result's `extra_usage`, recorded right after the
  tool-call chunk that produced it. Two cases feed this: a `spawn_subagent`
  or `make_agent_tool`-based tool's nested `AgentLoop` spend, or a tool like
  `ask_artifact` ([`tools.md`](tools.md#ask_artifact)) that makes a single
  side LLM call directly from its own handler, no nested `AgentLoop`
  involved. Both are tagged the same way — real spend beyond the main
  conversation's own turns, whichever shape it came in.
- **`"memory"`** — `_extract_memory_from`'s own usage, from the small
  tool-only `AgentLoop` that reviews a just-archived compaction transcript
  for anything worth remembering about the user (`memory/extraction.py`) —
  see [`memory.md`](memory.md#autonomous-extraction-derived). Recorded once
  per LLM call that sub-loop makes, right after the compaction pass that
  triggered it finishes. Uses the same `_effective_compaction_model()` as
  the compaction call above, for both the request itself and its cost tag.

**This only changes which entry represents "the main conversation" — it does
not change cost totals.** `session_total_usd` and `total_tokens` still
accumulate every recorded `TurnCost` regardless of `source`; subagent and
compaction spend is real money/tokens and was never excluded from the
running totals or the global ledger. What changed is `cost/context.py`'s
`current_context_usage`, `compute_max_response_tokens`, and
`looks_like_context_ceiling` — see [Context-window
tracking](#context-window-tracking) below and
[`configuration.md`](configuration.md#dynamic-response-cap) — which use "the
most recent entry" as a proxy for "how full is the main conversation's
context window," and now filter to `source == "main"` first rather than
trusting the literal last entry in `Session.cost.turns`. Before this, a
compaction call's usage (guaranteed to be the literal last entry every time
compaction ran, since `_run_compaction` records its own usage right after
the real turn finishes) or a subagent's usage (possible in an edge case)
could stand in for the main conversation's size, even though it reflects a
completely unrelated conversation.

### Session cost-budget enforcement

Everything above only *reports* cost. `Settings.max_session_cost_usd`
(`src/pcli/config/settings.py`, env var `PCLI_MAX_SESSION_COST_USD`) is a
hard cap on `Session.cost.session_total_usd` — the same running total
described above, which already includes subagent/compaction/memory-
extraction spend folded in. `None` (the default) means no cap: spend is
still tracked and reported exactly as before, just never enforced — this is
session-scoped only, not a daily or cross-session budget against the global
ledger (`cost_ledger_file()`).

`cost_budget_reason(session, max_session_cost_usd) -> str | None`
(`src/pcli/cost/tracker.py`) is the one place that owns the check and its
wording: `None` if there's no cap or spend is still under it, otherwise a
formatted reason naming the amount spent, the cap, and how to raise it
(`/budget <amount>` in the TUI, otherwise `PCLI_MAX_SESSION_COST_USD` or
`config.toml`).

`AgentLoop.run_turn` (`src/pcli/agent/loop.py`) takes an optional
`budget_check: Callable[[], str | None] | None = None` parameter, consulted
at the **top of every internal loop iteration** — the same `while True:`
spot as the pre-existing `max_tool_iterations` check — not just once per
turn. This matters because a single tool-call-heavy turn can accumulate a
lot of spend across many internal round-trips before ever returning control
to the caller. When `budget_check()` returns a reason, `run_turn` stops the
turn exactly the way the `max_tool_iterations` check does: yields a
`TextDelta` with the note, appends it as an assistant `ChatMessage`, sets
`terminated_early = True`, and breaks the loop. Because of this,
`HeadlessTurnResult.terminated_early`, `pcli run`'s non-zero exit code, and
`ScheduleJob.last_status` all react to a budget stop automatically, with no
new plumbing needed.

`budget_check` is a plain injected callable (matching the existing
`ask`/`tool_context_factory` injection pattern) rather than `AgentLoop`
importing `Session`/`Settings` itself — each caller closes over its own
`Session` and cap to build one via `cost_budget_reason`. It's threaded into
all four places that construct an `AgentLoop` and call `run_turn`:

- **The main TUI loop** — `ChatScreen._run_one_turn` (`tui/screens/chat.py`),
  closing over `self._session` and `self._settings.max_session_cost_usd`.
- **Headless/scheduled runs** — `run_headless_task` (`agent/headless.py`),
  closing over `session` and `settings.max_session_cost_usd`.
- **A subagent's own nested loop** — `run_nested_agent`
  (`tools/_nested_agent.py`), via `ctx.session`/`ctx.max_session_cost_usd`.
- **Memory extraction** — `extract_memory` (`memory/extraction.py`), the
  same `ctx.session`/`ctx.max_session_cost_usd` shape.

The latter two matter as much as the first two: subagent and memory-
extraction spend already fold into the *same* `session.cost.session_total_usd`
(see "Recording cost" above), so without their own `budget_check`, a
subagent could blow straight through the budget in its own internal
iterations while the parent loop's own check sits paused, waiting for that
tool call to return.

`ToolContext` (`tools/base.py`) carries `max_session_cost_usd: float | None
= None` for exactly this purpose — how a subagent's or memory-extraction's
nested loop gets the same cap the parent loop was given. It's populated by
`make_tool_context` (`agent/runtime.py`) and `ChatScreen._make_tool_context`
from `settings.max_session_cost_usd`.

**Changing the cap:**

- In the TUI, `/budget [amount|off]` (see
  [`tui-guide.md`](tui-guide.md#slash-commands)) views or sets it live,
  persisting to `config.toml` the same way `/temperature` does.
- For a single headless/scheduled invocation without touching the persisted
  setting, `pcli run --max-cost <amount>` and `pcli schedule add --max-cost
  <amount>` (`ScheduleJob.max_cost_usd`) apply a local override for just that
  run/job — see [`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md#pcli-run-one-shot-task-execution)
  and [`scheduling.md`](scheduling.md).

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
`usage.total_tokens` from the last `source="main"` entry in
`Session.cost.turns` (prompt + completion tokens of the last actual main-
conversation LLM call, skipping over any `"subagent"`/`"compaction"` entries
that may sit after it — see [`TurnCost.source`](#turncostsource) above),
which is exactly the size of what gets resent as history on the next call.
The status bar's `ctx: used/limit (pct%)` is driven specifically by the
*parent* loop's own usage events (never a subagent's smaller, isolated
context) — see `ChatScreen._refresh_context_display`.
