# Built-in tools

`build_default_registry()` (`src/pcli/tools/registry.py`) registers these
tools into every session's `ToolRegistry`. Toolbox-discovered tools
(kubectl, SGE, etc.) are merged in separately — see
[`toolbox-plugins.md`](toolbox-plugins.md).

Every tool call goes through `AgentLoop._dispatch_tool_call`
(`src/pcli/agent/loop.py`): JSON-schema validation of arguments, then
`PermissionManager.check` (guardrails first, then remembered
grants/ask-the-user — see [`sandbox-and-permissions.md`](sandbox-and-permissions.md)),
then the handler runs, then the result passes through the artifact-archiving
choke point described below before going back to the model.

Each entry lists: what it does, its JSON-schema parameters, whether it
prompts for permission (`needs_permission`), and any guardrail hook.

## read_file

Reads a text file. Resolves relative paths against the working directory,
reads up to 512,000 bytes (`_MAX_READ_BYTES`), appends `[...truncated...]` if
the file is larger, and decodes with `errors="replace"`.

- **Parameters:** `path` (string, required).
- **Permission:** not required (`needs_permission=False`).
- **Guardrail:** `path` is checked against `fs_allowed_roots`/`fs_deny_paths`
  regardless.

## write_file

Overwrites (or creates) a text file, creating parent directories as needed.

- **Parameters:** `path` (string, required), `content` (string, required).
- **Permission:** required. `risk_description`: "Writes/overwrites a file on
  disk."
- **Guardrail:** `path` checked against the fs allow/deny lists.

## list_dir

Lists a directory's immediate entries as `d  name` / `f  name` lines, sorted
by name.

- **Parameters:** `path` (string, optional, default: working directory).
- **Permission:** not required.
- **Guardrail:** `path` checked.

## glob_search

Finds files under a base directory matching a glob pattern (e.g. `**/*.py`).
Returns up to 500 matches (sorted, relative to the base), with a
"more matches not shown" note if truncated.

- **Parameters:** `pattern` (string, required), `path` (string, optional
  base directory).
- **Permission:** not required.
- **Guardrail:** `path` checked.

## grep

In-process regex search across text files under a directory (own
implementation, not a wrapper around system `grep`/`ripgrep`). Scans up to
5,000 files (`_MAX_FILES_SCANNED`) matched by an optional `glob` (default
`**/*`), returns up to 200 matches (`_MAX_MATCHES`) as `path:line: text`
(each line clipped to 200 chars), reading files as UTF-8 with
`errors="ignore"` and silently skipping unreadable files.

- **Parameters:** `pattern` (string regex, required), `path` (string,
  optional base directory), `glob` (string, optional file filter).
- **Permission:** not required.
- **Guardrail:** `path` checked.

## run_shell

Runs a shell command through `ctx.sandbox` — the only builtin tool that
actually goes through the `Sandbox` abstraction (Docker or restricted
subprocess; see [`sandbox-and-permissions.md`](sandbox-and-permissions.md)).
Output (`stdout` + `--- stderr ---` + `stderr`) is capped at 100,000 chars
(`_MAX_OUTPUT_CHARS`). Result is flagged as an error if the exit code is
nonzero or the command timed out.

- **Parameters:** `command` (string, required), `timeout_s` (number,
  optional, default 30).
- **Permission:** required. `needs_sandbox=True`. `risk_description`:
  "Executes an arbitrary shell command."
- **Guardrail:** `command` checked against the shell denylist.

## search_python

"Tier 1" Python package/module discovery: searches a cached name+summary
index (built from `pkgutil`/`importlib.metadata`, never imports anything) by
substring match against name or summary. Returns up to 50 results
(`_MAX_RESULTS`). The index is rebuilt automatically when the environment's
fingerprint (interpreter path + installed distributions) changes; otherwise
it's read from `cache_dir()/pydiscovery_index.json`.

- **Parameters:** `query` (string, required; empty string lists everything).
- **Permission:** not required — nothing is imported.

## inspect_python_module

"Tier 2": actually imports one specific module (in an isolated subprocess,
via `run_bootstrap`/`RestrictedSubprocessSandbox`, never the main pcli
process) and lists its public functions/classes with signature and
first-line docstring, optionally filtered by a `query` substring. Capped at
200 members.

- **Parameters:** `module` (string, required — fully-qualified name),
  `query` (string, optional filter).
- **Permission:** required. `needs_sandbox=True`. `risk_description`:
  "Imports a Python module, which may execute module-level side effects."
- **Guardrail:** the module's top-level name is checked against
  `python_module_denylist` (default: `os`, `sys`, `subprocess`, `ctypes`,
  `shutil`, `socket`, `importlib`, `multiprocessing`, `threading`, `pty`).

## call_python

Calls a Python function by fully-qualified name (e.g. `math.sqrt`) with
JSON-serializable args/kwargs, in the same isolated subprocess mechanism as
`inspect_python_module` (deliberately *not* through `ctx.sandbox`, since that
may be a bare Docker image lacking the host's installed packages — pydiscovery's
whole point is reaching packages installed in the *host* environment).
Non-JSON-serializable results are returned as `<type> repr(...)` (repr
capped at 2000 chars).

- **Parameters:** `qualified_name` (string, required), `args` (array,
  optional), `kwargs` (object, optional).
- **Permission:** required. `needs_sandbox=True`. `risk_description`:
  "Executes an arbitrary Python function call in a subprocess."
- **Guardrail:** `qualified_name`'s top-level module checked against the same
  `python_module_denylist`.

## spawn_subagent

Delegates a focused task to a fresh, independent `AgentLoop` sharing the
parent's gateway/permissions. Only the subagent's final text and tool-call
count are returned to the parent — its own tool calls aren't individually
recorded in the parent session, though they still pass through the same
guardrail/permission gates. The subagent's own LLM usage is folded into cost
tracking via `ToolResult.extra_usage`.

**Subagents can never spawn further subagents** — `spawn_subagent` is always
filtered out of the tool registry a subagent runs with (by name, in
`_spawn_subagent`), so nesting is capped at exactly one level by construction.

- **Parameters:** `task` (string, required), `allowed_tools` (array of
  strings, optional — restricts the subagent's toolset), `max_iterations`
  (integer, optional).
- **Iteration cap:** `min(requested_max_iterations, 15)` if given, else
  `min(ctx.max_tool_iterations, 15)` — 15 is `_DEFAULT_MAX_ITERATIONS`.
- **Permission:** required. `risk_description`: "Spawns a subagent that can
  call tools (including sandboxed ones) on its own."
- **Live progress:** while running, reports task/tool-call-count/last-tool to
  `ActivityTracker`, which the TUI's status pane renders (see
  [`tui-guide.md`](tui-guide.md)).

## write_todos

Lets the model maintain a structured task list for the session. Each call
**replaces the whole list** (not incremental deltas) — stored on
`Session.todos`, so it persists/exports/imports like everything else. Rejects
more than one `in_progress` item at a time.

- **Parameters:** `todos` (array of `{content: string, status: "pending" |
  "in_progress" | "completed"}`, required).
- **Permission:** not required.
- Requires `ctx.session` to be set (errors otherwise).

## fetch_artifact

Retrieves (a windowed slice of) content previously archived by the automatic
truncation mechanism below.

- **Parameters:** `artifact_id` (string, required), `offset` (integer,
  optional, default 0), `limit` (integer, optional, default 4000).
- **Permission:** not required.
- Returns the `[offset:offset+limit]` slice plus a footer noting the next
  offset to use if more remains.

## Artifact archiving

This is automatic infrastructure inside `AgentLoop`, **not** something the
model decides to invoke — every tool result passes through
`AgentLoop._archive_if_large` (`src/pcli/agent/loop.py`) before being appended
to the conversation:

1. If `len(output) <= artifact_threshold_chars` (default 4000, see
   [`configuration.md`](configuration.md)) or no `ArtifactStore` is
   configured, the output passes through unchanged.
2. Otherwise, the *full* output is archived via
   `ctx.artifact_store.put(output)` (backed by `SessionArtifactStore`, which
   writes it as a blob under the session's `blobs/` directory — see
   [`sessions-and-cost.md`](sessions-and-cost.md)), returning an
   `artifact_id` (e.g. `art_...`).
3. The message actually sent to the model is a preview —
   `min(2000, artifact_threshold_chars)` characters (`_ARTIFACT_PREVIEW_CHARS`)
   — followed by a note: `[...output truncated: N chars total, archived as
   artifact_id='art_...'. Call fetch_artifact(artifact_id='art_...') if you
   need the rest...]`.

The model can then call `fetch_artifact` to page through the archived content
with `offset`/`limit`. The system prompt (`src/pcli/agent/prompt.py`)
instructs the model not to assume it's seen the whole result when this note
appears, but also not to reflexively re-fetch whole artifacts back into
context. Because the artifact store is session-backed, an `artifact_id` also
serves as a `ToolInvocation.full_result_ref` — the chat screen points a
session's tool-invocation record at the same blob rather than storing the
full output twice (`ChatScreen._record_tool_invocation`).

## Auto-compaction

The same archiving idea applied one level up: to whole conversation history
instead of a single oversized tool result. Implemented in
`agent/compaction.py`'s `async def maybe_compact(session, *, gateway_client,
model, artifact_store, keep_recent_turns=2)`, called from `ChatScreen` (see
[`tui-guide.md`](tui-guide.md#slash-commands) for the `/compact` command and
the automatic trigger, and [`configuration.md`](configuration.md#auto-compaction)
for the settings) rather than from `AgentLoop` — it needs a dedicated LLM
call and session-level access to cut turn boundaries, which per-tool-result
archiving doesn't.

- **What gets compacted:** the oldest messages, cut only at `role=="user"`
  boundaries (`_turn_boundaries`) so an assistant `tool_calls` message is
  never separated from its matching tool-result message. The
  `keep_recent_turns` most-recent user turns (default 2, from
  `auto_compact_keep_recent_turns`) are always left verbatim, and the leading
  system prompt at `messages[0]` is never touched. A *previous* compaction's
  own summary message is also `role=="system"` but doesn't sit at index 0, so
  it stays eligible to be folded into a later compaction — see the docstring
  on `_system_prompt_prefix_len` for why only `messages[0]` is special-cased,
  not a run of leading system messages.
- **How:** the messages being compacted are rendered to plain text
  (`_render_transcript`) and archived via `ArtifactStore.put()` — the exact
  same store/mechanism as the artifact archiving above — then summarized with
  one dedicated, non-streaming `GatewayClient.collect()` call (a system
  prompt instructing the model to capture what was asked, what's been done,
  and what's still pending). The compacted range is replaced in
  `session.messages` with a single `role="system"` message holding the
  summary plus a note referencing the archived `artifact_id`.
- **Retrieval:** `fetch_artifact` (above) works unchanged against a
  compaction's `artifact_id` — it's just another entry in the same
  `ArtifactStore`; no new tool was added for this.
- **No-op guard:** `maybe_compact` returns `None` (nothing to do) once there
  are `keep_recent_turns` or fewer user turns in the session; `/compact`
  surfaces this as "Nothing to compact yet."
