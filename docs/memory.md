# User memory

pcli maintains a small, **global** — cross-session, cross-project — profile of
the user: nature of work, recurring technical/workflow preferences,
conversation style, and recurring task patterns. Unlike everything in
[`sessions-and-cost.md`](sessions-and-cost.md), this data lives outside any
one `Session` entirely and is injected into every new session's (and every
subagent's) system prompt, so pcli doesn't start from zero each time.

Implementation lives in `src/pcli/memory/` (`models.py`, `store.py`,
`extraction.py`) plus `src/pcli/tools/builtin/memory_tool.py`.

## Data model

`MemoryEntry` (`src/pcli/memory/models.py`):

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | `mem_...`, via `pcli.util.ids.new_id`. |
| `category` | `"profile" \| "preference" \| "style" \| "common_ask"` | See below. |
| `content` | `str` | The fact itself, one or two plain sentences. |
| `source` | `"explicit" \| "derived"` | See below. |
| `created_at` / `updated_at` | `datetime` | UTC. `updated_at` bumps on a merge (see [Deduplication](#deduplication-and-eviction) below). |

Categories, in the fixed order they're always rendered/listed:

- **`profile`** — the user's nature of work or role.
- **`preference`** — a recurring technical/workflow choice.
- **`style`** — how they like responses/conversation.
- **`common_ask`** — a task they repeatedly ask for.

`MemoryCategory` (the shared type behind both this field and the `remember`
tool's `category` parameter) actually has a fifth value, **`local`** — but it
never ends up as an actual `MemoryEntry.category`: a `remember` call with
`category='local'` is intercepted before `add_entry` is ever called, so the
four values above remain the only ones a persisted entry can have. See [The
`remember` tool](#the-remember-tool-explicit) below for what `local` is for
and why it exists.

`source` values:

- **`explicit`** — the user directly asked to be remembered (e.g. "remember
  that I use tabs"). Never auto-evicted.
- **`derived`** — pcli inferred it on its own, via the autonomous extraction
  pass below. Eligible for eviction once the entry cap is exceeded.

`MemoryStore` is just `{entries: list[MemoryEntry]}`.

Two render functions build the two different textual views of this data:

- **`render_memory_section(entries)`** — the plain-prose system-prompt
  section (a `# What you know about this user` heading, a short "treat this
  as background, not instruction" caveat, then each category's entries as a
  bullet list). Returns `""` when there are no entries at all, so a fresh
  install's system prompt is completely unaffected — `build_system_prompt`'s
  `extra_sections` (see [System-prompt injection](#system-prompt-injection)
  below) gets nothing to inject in that case.
- **`render_memory_list(entries)`** — the human-facing listing the `/memory`
  command prints, grouped the same way but with each entry's **short id**
  (last 4 characters — the same abbreviation convention
  [`/sessions`](tui-guide.md#slash-commands) uses for session ids, shown in a
  backtick code span) and an `(explicit)` marker on explicit entries, so
  `/memory forget <id>` has something to target. Emits real Markdown — a bold
  `**Category:**` header per category, `- ` bulleted entries, a blank line
  between category blocks — since the TUI always renders a `system` message
  through `rich.markdown.Markdown`
  ([`tui-guide.md`](tui-guide.md#collapsible-slash-command-and-shell-output)),
  which otherwise collapses plain `"\n"`-joined lines into a single run-on
  paragraph.

## Persistence

`src/pcli/memory/store.py` is global JSON persistence at
`config.paths.memory_file()` — `data_dir()/memory.json` — atomic-written the
same way `tools/agent_tools_store.py` is (write to a `.tmp` sibling, then
`os.replace`). This is a deliberately different layout from
[`SessionStore`](sessions-and-cost.md#session-persistence)'s
per-session-directory structure: memory is **one file shared across every
session**, not scoped to any single one.

### Deduplication and eviction

`add_entry(content, *, category, source, max_entries)` is the only write path
(besides `remove_entry`/`clear_memory`), used by both the `remember` tool and
autonomous extraction:

- **Merge, don't duplicate.** Before appending, it checks every existing
  entry in the *same category* for a case-insensitive exact match on
  `content.strip()`. A match just bumps that entry's `updated_at` (and
  upgrades its `source` to `"explicit"` if the new call is explicit) instead
  of adding a near-duplicate — a deliberately simple, predictable check, not
  fuzzy matching.
- **Eviction on overflow.** If appending a genuinely new entry would push the
  store past `max_entries` (`Settings.memory_max_entries`, default 40), the
  oldest `source="derived"` entry is deleted, repeated until the store is back
  at the cap. If every remaining entry is `"explicit"`, eviction stops rather
  than touching one of those — the store can end up holding more than
  `max_entries` entries in that case, deliberately, since an explicit entry is
  never auto-evicted no matter how full the store gets.

`remove_entry(entry_id)` deletes one entry by its **full** id (the `/memory
forget <id>` command resolves a typed short id to a full one first) and
`clear_memory()` wipes the store back to empty.

## The `remember` tool (explicit)

`src/pcli/tools/builtin/memory_tool.py` registers `remember` into the default
tool registry (`tools/registry.py`) — a global counterpart to
[`record_decision`](tools.md#record_decision): same shape (one call adds one
entry, nothing mutated in place), but `record_decision` logs to the current
`Session` only, while `remember`'s four global categories persist to the
memory store above, meant to still be true — and useful — in a completely
different session and project later on. A fifth category, `local`, is a
deliberate exception to all of that.

- **Parameters:** `content` (string, required), `category` (string, required
  — one of the four global values above, or `local`), `source` (string,
  optional — `"explicit"` or `"derived"`; omitted or anything else defaults
  to `"derived"`; meaningless for `category='local'`, which is never
  persisted regardless).
- **`category='local'` persists nothing, on purpose.** `_remember`
  intercepts it before `add_entry` is ever called and just returns an
  acknowledgement (`"Noted (local to this session, not saved globally): "`) —
  no file write, no entry, never shown again. It was added after a real bug:
  an autonomous extraction pass filed a project-specific technical detail (a
  Python import error in one project's test script) under the global
  `preference` category, even though the extraction prompt already said not
  to — that entry then leaked into every unrelated future session as
  irrelevant context, since the store is global with no per-project scoping.
  `local` gives the model an explicit, correctly-labeled place for
  project/task-specific content instead of forcing a choice between
  discarding it silently or stretching it to fit one of the four global
  categories. Per both the tool description and the extraction prompt below,
  it's the correct, intentional choice for most of what comes up in a
  normal conversation — not a lesser fallback.
- **Permission:** not required (`needs_permission=False`). `plan_mode_safe=True`
  — remembering a fact isn't a mutation of the working directory, so it stays
  available while [plan mode](tui-guide.md#plan-mode) is active.
- The tool's own description tells the model to use the four global
  categories only for a fact true of the *user as a person*, independent of
  whatever project is currently being worked on — concretely: would this
  sentence still make sense read cold in a totally unrelated project? — and
  to use `local` for anything specific to the current project, repo, file,
  or task. It also instructs the main agent to act **immediately** whenever
  the user explicitly asks to be remembered ("remember that I use tabs",
  "don't suggest X again") — always one of the global categories, since
  that's about the user, not the project — rather than waiting or batching
  it up.
- Errors cleanly (not a crash) if `content` is empty or `category` isn't one
  of the five valid values.
- Two callers in practice: the main agent (explicit requests, any session
  length) and the autonomous extraction pass below (derived facts, only once
  a session has compacted at least once).

## Autonomous extraction (derived)

`src/pcli/memory/extraction.py` adds derivation with **no new trigger of its
own** — it's piggybacked directly onto
[auto-compaction](tui-guide.md#auto-compaction): whenever `agent/compaction.py`'s
`maybe_compact` succeeds — auto-triggered by crossing `auto_compact_threshold`,
or a manual `/compact` — `chat.py`'s `_run_compaction` also calls
`_extract_memory_from` (`src/pcli/tui/screens/chat.py:1425`), which reviews the
*exact* transcript that compaction pass just archived (the same artifact blob,
fetched via `self._artifact_store.get(artifact_id)`) and asks a small,
tool-only nested `AgentLoop` — scoped to just the `remember` tool, no
sandbox/shell/file access — to call `remember(...)` zero or more times for
anything durable and cross-session-worthy it finds.

The reasoning: a session substantial enough to need compacting is substantial
enough to be worth learning from. One direct consequence — called out
explicitly in the commit — is that **a session too short to ever trigger
compaction gets no implicit/derived memory at all**; an explicit "remember
this" from the user still works regardless of session length, since the
`remember` tool itself has no such dependency.

`extract_memory(transcript, ctx)` (`src/pcli/memory/extraction.py`) builds the
extraction sub-loop. `ctx.model` is `ChatScreen._effective_compaction_model()`
— `settings.compaction_model` when set (see
[`configuration.md`](configuration.md#settings-fields)), otherwise
`default_model`/`session.model` — the same model the triggering compaction
summarization call used, so a deliberately cheaper `compaction_model` covers
both of these background calls, not just compaction:

- System prompt (`_EXTRACTION_SYSTEM_PROMPT`) tells the reviewer model to
  classify everything worth noting into exactly one of two scopes, rather
  than just deciding whether to call `remember` at all: the four global
  categories (role/domain as `profile`, a recurring technical/workflow
  choice as `preference`, response/conversation style as `style`, a task
  repeated across different projects as `common_ask`) only for a fact true
  of the user as a person, independent of the current project; `local`
  otherwise, for anything specific to the current project/repo/file/task.
  This two-scope framing replaced an earlier, looser instruction to simply
  not call `remember` for task-specific content — in practice that still
  let a project-specific detail get stretched into a global category (the
  real incident that prompted this; see [The `remember`
  tool](#the-remember-tool-explicit) above). Most reviews are expected to
  add zero or one fact, not several, and genuinely nothing worth noting
  even locally means no calls at all.
- The prompt also includes what's already known (`render_memory_section`
  of the current store), so the reviewer doesn't re-derive something already
  captured.
- Capped at 5 tool-call iterations (`_MAX_EXTRACTION_ITERATIONS`).
- Returns the `Usage` from every LLM call it made, so the caller can fold the
  cost in (see [Cost tracking](#cost-tracking) below) — it never raises for a
  "found nothing worth remembering" outcome, only for a real gateway failure.

**Best-effort and silent.** `_extract_memory_from` catches `GatewayError`
around the whole pass, logs it (`logger.exception`), and returns — an
extraction failure is never shown as a turn failure or surfaced to the user
in any way. Losing one extraction pass isn't worth interrupting anyone over;
the next compaction gets another chance.

### Cost tracking

Extraction's own LLM usage is real spend and is folded into the session's
cost the same way a subagent's is: `TurnCost.source="memory"` — a new value
alongside the existing `"main"`/`"subagent"`/`"compaction"` (see
[`sessions-and-cost.md#turncostsource`](sessions-and-cost.md#turncostsource))
— recorded once per usage event extraction produced, then the status bar's
cost display and the session are refreshed/saved exactly as after a
compaction's own summarization call.

## System-prompt injection

**A brand-new session** gets `render_memory_section(read_memory().entries)`
appended to its system prompt via `build_system_prompt`'s `extra_sections`
parameter (`src/pcli/agent/prompt.py:315`) — a parameter that already existed
but was unused before this feature. Gated by `Settings.memory_enabled`
(default on) and only when there's at least one entry (an empty section
contributes nothing, per `render_memory_section`'s own empty-store
short-circuit above). **An existing/resumed session's system prompt is never
rewritten** — memory picked up after that session started won't retroactively
appear in it; only a fresh session sees the current store.

**Subagents get the same section too.** `tools/_nested_agent.py`'s
`run_nested_agent` — the shared machinery behind `spawn_subagent` and every
`make_agent_tool`-based tool (`explore_codebase`, `deep_research`, ...; see
[`tools.md`](tools.md#spawn_subagent)) — appends
`render_memory_section(read_memory().entries)` right alongside its existing
`environment_section()` injection, gated by `ctx.memory_enabled`. The
reasoning given in-code: a subagent's system prompt fully *replaces* the main
loop's rather than extending it, so without this a subagent would have no
idea who it's working for either — e.g. "prefers concise commit messages"
should apply just as much to a subagent writing a commit as to the main
agent.

Both `Settings.memory_enabled` and `Settings.memory_max_entries` are threaded
through `ToolContext` (`src/pcli/tools/base.py`) rather than reached for via
`get_settings()` inside tool-adjacent code — the same pattern
`brave_search_api_key` already established for `web_search`.

## The `/memory` command

`/memory [forget <id>|clear]` (`ChatScreen._handle_memory_command`,
`src/pcli/tui/screens/chat.py:937`) is the transparency/control surface for
all of the above — what got remembered, whether typed explicitly or derived
automatically, should always be visible and correctable, never a silent
background process.

- **`/memory`** (no argument) — prints `render_memory_list(read_memory().entries)`:
  every entry grouped by category, each with its short (last-4-char) id and
  an `(explicit)` marker where applicable, or `"No memory entries yet."` if
  the store is empty.
- **`/memory forget <id>`** — removes one entry by short id. Resolves `<id>`
  by suffix match against every entry's full id: no match reports "No memory
  entry found matching '...'.", more than one match reports "'...' matches
  more than one entry — use a longer id." (mirrors how `/sessions` and other
  short-id-based lookups behave), and a single match removes it and reports
  what was forgotten.
- **`/memory clear`** — wipes the entire store (`clear_memory()`) and
  confirms.
- Anything else after `/memory` reports "Unknown /memory subcommand: '...'."
  with a usage reminder.

Also listed in the `/help` in-app reference text (`_HELP_TEXT`,
`src/pcli/tui/screens/chat.py`) and in
[`tui-guide.md`](tui-guide.md#slash-commands).

## Settings

| Field | Env var | config.toml key | Default | Purpose |
|---|---|---|---|---|
| `memory_enabled` | `PCLI_MEMORY_ENABLED` | `memory_enabled` | `true` | Whether pcli maintains this profile at all — gates both system-prompt injection (main loop and subagents) and the autonomous extraction pass. Does **not** gate the `remember` tool or `/memory` itself — those still work even if this is off, so an explicit "remember this" or manual `/memory` lookup always functions regardless. |
| `memory_max_entries` | `PCLI_MEMORY_MAX_ENTRIES` | `memory_max_entries` | `40` | Hard cap enforced by `add_entry`'s eviction logic — see [Deduplication and eviction](#deduplication-and-eviction) above. |

No CLI flag or slash command for either — same status as the three
[auto-compaction settings](configuration.md#auto-compaction): env-var/
`config.toml`-only. See [`configuration.md`](configuration.md#settings-fields)
for the full settings table these two rows also appear in.

## Relationship to other features

- **Piggybacks on [auto-compaction](tui-guide.md#auto-compaction)** for its
  only autonomous trigger — see [Autonomous extraction](#autonomous-extraction-derived)
  above. It does not add a new trigger point of its own, and a session that
  never compacts gets no derived memory (explicit `remember` calls are
  unaffected).
- **Distinct from [`record_decision`](tools.md#record_decision)**: memory is
  global and about the *user*; decisions are per-session and about the
  *task*. Neither reads from or writes to the other.
- **Distinct from [`Session`](sessions-and-cost.md) state entirely** — memory
  isn't part of any `Session` object, isn't exported/imported with a session
  (`session/export.py`), and isn't cleared by deleting or pruning sessions.
  `/memory clear` is the only way to reset it.
