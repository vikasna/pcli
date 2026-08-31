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
- **File-not-found** gets an appended suggestion to try `list_dir` on the
  parent directory or `glob_search` if the exact path isn't certain
  (`src/pcli/tools/builtin/fs_tools.py:23`).

## write_file

Overwrites (or creates) a text file, creating parent directories as needed.

- **Parameters:** `path` (string, required), `content` (string, required).
- **Permission:** required. `risk_description`: "Writes/overwrites a file on
  disk."
- **Guardrail:** `path` checked against the fs allow/deny lists.

## edit_file

Replaces an exact, unique occurrence of `old_string` with `new_string` in an
existing file — for a small change, without resending the whole file the way
`write_file` requires. `old_string` must match the file's current content
exactly (including whitespace) and occur exactly once.

- **Parameters:** `path` (string, required), `old_string` (string, required),
  `new_string` (string, required).
- **Permission:** required. `risk_description`: "Edits a file on disk."
- **Guardrail:** `path` checked against the fs allow/deny lists, same as
  `write_file`.
- **Errors cleanly** (not a crash) in three cases
  (`src/pcli/tools/builtin/fs_tools.py:79`): the file doesn't exist (message
  points at `write_file` to create it instead), `old_string` isn't found
  (appends a suggestion to `read_file` first, in case an earlier
  `edit_file`/`write_file` call already changed that part), or `old_string`
  matches more than once (asks for more surrounding context to disambiguate).

## list_dir

Lists a directory's immediate entries as `d  name` / `f  name` lines, sorted
by name.

- **Parameters:** `path` (string, optional, default: working directory).
- **Permission:** not required.
- **Guardrail:** `path` checked.
- **Not-a-directory** gets an appended suggestion to `list_dir` the parent
  to confirm the correct name/path (`src/pcli/tools/builtin/fs_tools.py:138`).

## glob_search

Finds files under a base directory matching a glob pattern (e.g. `**/*.py`).
Returns up to 500 matches (sorted, relative to the base), with a
"more matches not shown" note if truncated.

- **Parameters:** `pattern` (string, required), `path` (string, optional
  base directory).
- **Permission:** not required.
- **Guardrail:** `path` checked.
- **Not-a-directory** gets the same `list_dir`-the-parent suggestion as
  `list_dir` above (`src/pcli/tools/builtin/fs_tools.py:167`).

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
- **Invalid regex** gets an appended suggestion to escape the special
  character(s) or fall back to a plain substring search if regex features
  aren't actually needed; **not-a-directory** gets the same `list_dir`-the-
  parent suggestion as above (`src/pcli/tools/builtin/grep_tool.py:28`).

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
- **Guardrail:** `command` checked against the shell denylist. `timeout_s` is
  also clamped to the guardrail ceiling `limits.max_shell_timeout_s` (default
  300s, see [`sandbox-and-permissions.md`](sandbox-and-permissions.md#guardrails));
  if the requested value exceeds it, the timeout is silently clamped and the
  tool output notes the clamp and points at `run_shell_background` instead
  (`src/pcli/tools/builtin/shell_tool.py:20`).
- **Failures include a corrective suggestion where one exists**
  (`src/pcli/tools/builtin/shell_tool.py:24`), appended to the error output
  as `\n[pcli] Suggestion: ...`:
  - **Timeout:** if `timeout_s` was below the guardrail ceiling, suggests
    raising it; if already at/near the ceiling, suggests
    `run_shell_background` instead (since it doesn't block the turn) — e.g.
    a command run with `timeout_s=30` that times out gets "the command timed
    out at timeout_s=30s. Try raising timeout_s (up to the guardrail ceiling
    of 300s) if it just needs more time, or use run_shell_background if it
    has no natural end...".
  - **Nonzero exit:** the combined stdout/stderr is pattern-matched
    (`_shell_failure_suggestion`) against three failure signatures confirmed
    from a real debugged session — bare `pip` not recognized (→ use
    `python -m pip`), the Windows "`python3` not found"/Microsoft Store stub
    error (→ use plain `python`), and `ModuleNotFoundError` (→ check the
    active interpreter via `python -c "import sys; print(sys.executable)"`,
    since a prior successful `pip install` doesn't guarantee it targeted the
    interpreter actually running the script). An unmatched failure gets no
    suggestion appended.

## run_shell_background, read_background_output, stop_background_process

Background execution for commands with no natural end (dev servers,
watchers) or that may run longer than a few minutes — instead of blocking
the turn on a very large `run_shell` `timeout_s`. All three are only
registered when the active sandbox backend is `subprocess`
(`RestrictedSubprocessSandbox`, see
[`sandbox-and-permissions.md`](sandbox-and-permissions.md#sandbox-backends));
calling any of them with Docker or `none` active returns a clean error
naming the active backend, not a crash
(`src/pcli/tools/builtin/shell_tool.py:14`).

**`run_shell_background`** starts a command and returns immediately with a
`job_id` instead of waiting for it to finish.

- **Parameters:** `command` (string, required).
- **Permission:** required. `needs_sandbox=True`. `risk_description`:
  "Starts a shell command that keeps running in the background."
- **Guardrail:** `command` checked against the shell denylist, same as
  `run_shell`. Concurrent background jobs are capped at guardrail
  `limits.max_background_jobs` (default 5); starting one more than that
  raises rather than starting it.

**`read_background_output`** reports whether a job is still running or has
exited (with exit code), and returns only the *new* stdout/stderr produced
since the last read of that job — offset-based, not a fixed tail, so nothing
in the middle is silently dropped between polls
(`src/pcli/sandbox/subprocess_backend.py:276`). If more than `max_chars` is
waiting, the result says so and the caller should call again rather than
requesting a larger `max_chars`.

- **Parameters:** `job_id` (string, required), `max_chars` (integer,
  optional, default 4000), `reset` (boolean, optional, default `false` — when
  `true`, re-reads everything from the start instead of continuing from the
  last offset).
- **Permission:** not required (`needs_permission=False`) — read-only.
  `needs_sandbox=True`.
- **Unknown `job_id`** gets an appended suggestion to re-check the "Started
  background job '...'" message from `run_shell_background`, since ids
  aren't recoverable any other way (`src/pcli/tools/builtin/shell_tool.py:18`,
  shared with `stop_background_process` below).

**`stop_background_process`** kills a background job.

- **Parameters:** `job_id` (string, required).
- **Permission:** required. `needs_sandbox=True`. `risk_description`:
  "Kills a running background process."
- **Unknown `job_id`** gets the same suggestion as `read_background_output`
  above.

## register_toolbox_tool

Lets the model itself trigger toolbox discovery/registration mid-conversation
— previously this was human-only, via `/toolbox discover` in the TUI or
`pcli toolbox discover` on the CLI (see
[`toolbox-plugins.md`](toolbox-plugins.md)). Intended use: the model writes a
small reusable script for something it expects to need repeatedly, then
registers it as a real callable tool instead of re-deriving the same shell
command every time. See
[`toolbox-plugins.md#self-authored-scripts`](toolbox-plugins.md#self-authored-scripts)
for the underlying `path` mechanics.

- **Parameters:** `name` (string, required — short identifier used as the
  new tool group's prefix), `path` (string, optional — path to a
  self-authored script; omit to look `name` up on `PATH` instead).
- **Permission:** required **unconditionally** — `needs_permission=True`
  with no exceptions, unlike other tools where a `risk` level can make a call
  default-allow. `risk_description`: "Registers a new tool the model can call
  in later turns — a bigger action than running one command, since it grants
  standing execution rights." This is also called out explicitly in the
  tool's description string and in the system prompt
  (`src/pcli/tools/builtin/toolbox_register_tool.py:35`).
- **Discovery failures** get an appended, situation-specific suggestion
  (`src/pcli/tools/builtin/toolbox_register_tool.py:25`): a `path` that
  wasn't found suggests `write_file`-ing the script first or double-checking
  the path; other failure shapes get their own targeted suggestion (e.g.
  checking the script has a working `--help`).

## register_agent_tool

Lets the model itself define a new named subagent persona — a fixed system
prompt plus a fixed, restricted set of already-existing tool names — callable
afterward as an ordinary tool taking one `query` argument. See
[`agent-tools-guide.md`](agent-tools-guide.md) for a how-to walkthrough
(creating one, a worked example, and scoping/`plan_mode_safe` guidance) — this
section is the parameter/behavior reference only. Built on the same
underlying mechanism as `spawn_subagent` and the three built-in
`explore_*` tools below (`make_agent_tool`,
`src/pcli/tools/agent_tools.py`), the difference being the persona/allowed-
tools are baked in once at registration time instead of being chosen by the
calling model on every call — useful when the model finds itself wanting to
delegate the same kind of focused sub-task repeatedly instead of re-spelling
the same instructions to `spawn_subagent` each time.

- **Parameters:** `name` (string, required — the tool name it'll be callable
  by afterward), `description` (string, required — shown to the calling
  model as the new tool's description), `persona_prompt` (string, required —
  system prompt for the nested subagent: its role and how it should approach
  its task), `allowed_tools` (array of strings, required — fixed set of
  *existing* tool names the nested subagent is restricted to),
  `plan_mode_safe` (boolean, optional, default `false` — whether the new
  tool should stay usable while [plan mode](tui-guide.md#plan-mode) is
  active; only meaningful set `true` if every tool in `allowed_tools` is
  itself read-only/exploration-only).
- **Permission:** required **unconditionally**, same stance as
  `register_toolbox_tool` above — `needs_permission=True` with no
  risk-based exception. `risk_description`: "Registers a new tool the model
  can call in later turns — grants standing execution rights to a nested
  subagent, a bigger action than running one command."
- **Unknown tool names** in `allowed_tools` (anything not already present in
  the caller's own tool registry) fail cleanly with a suggestion to only
  reference tools that actually exist, rather than registering a broken
  persona (`src/pcli/tools/builtin/agent_tool_register_tool.py:28`).
- **Persisted across restarts:** a successful registration is both saved to
  `agent_tools.json` in the data dir (`save_agent_tool`,
  `src/pcli/tools/agent_tools_store.py`) and registered live into the
  current session's tool registry, so it's callable immediately in the same
  session *and* still there after a restart. `ChatScreen.on_mount` calls
  `load_persisted_agent_tools()` at startup and merges every previously
  registered agent tool back into the registry, announcing "Loaded N
  previously-registered agent tool(s)." if any were found.
- Like `spawn_subagent`, a nested agent tool can never itself spawn further
  subagents or register more agent tools beyond what `allowed_tools`
  explicitly lists — and if the parent turn is in
  [plan mode](tui-guide.md#plan-mode), the nested subagent's own effective
  tool set is *also* filtered down to `plan_mode_safe` tools only
  (`make_agent_tool`'s `_allowed` check), so plan mode can't be bypassed by
  routing a mutating call through a nested agent tool.

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
- **Import/module-not-found failures** get an appended suggestion to run
  `search_python` first to confirm the exact module name is actually
  installed (`src/pcli/tools/pydiscovery/search.py:66`).

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
- **Call failures** get an appended, error-specific suggestion
  (`src/pcli/tools/pydiscovery/invoke.py:137`): an import/module-not-found
  error suggests `search_python` to confirm the exact module name; "not
  callable" suggests `inspect_python_module` to see what's actually callable
  there; other failures (bad args/types) suggest checking the signature via
  `inspect_python_module`.

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
  `ActivityTracker`, which the TUI's status bar renders as a second line (see
  [`tui-guide.md`](tui-guide.md)).
- **Failures get a suggestion, not just the raw error**
  (`src/pcli/tools/builtin/subagent_tool.py:39`): if subagents aren't
  available in this context (no gateway/tools/permissions configured), the
  suggestion is to handle the task directly instead of delegating; if the
  subagent itself raised an exception, the suggestion is to retry with a
  narrower task description or handle it directly.

## explore_codebase, explore_files, explore_logs

Three ready-made agent tools registered by default in
`build_default_registry()`, each a thin, fixed-persona/fixed-toolset wrapper
around the same nested-`AgentLoop` mechanism `spawn_subagent` uses
(`make_agent_tool`, `src/pcli/tools/agent_tools.py`) — the difference from
`spawn_subagent` is that the persona prompt and the allowed-tool set are
baked in ahead of time rather than supplied per-call by the model, so calling
one of these is a single-argument `query` call rather than spelling out a
task description, a persona, and an `allowed_tools` list every time. All
three run their nested subagent with `ctx.gateway_client`/
`ctx.permission_manager` shared from the parent, and — like
`spawn_subagent` — can never spawn further subagents or register more agent
tools themselves.

- **Parameters (all three):** `query` (string, required — a clear,
  self-contained description of what to look into and what to report back).
- **Permission (all three):** required. `risk_description`: "Runs a nested
  agent (`<name>`) that can call tools within its fixed allowed set on its
  own."
- **Live progress:** same `ActivityTracker`/status-bar reporting as
  `spawn_subagent` — see [`tui-guide.md`](tui-guide.md#status-bar).
- **`plan_mode_safe=True`** for all three (`src/pcli/tools/agent_tools.py`) —
  they stay available while [plan mode](tui-guide.md#plan-mode) is active,
  since every tool each one is restricted to is itself read-only.

**`explore_codebase`** — delegates a focused code-exploration question (e.g.
"how is auth implemented", "where is X defined") to a subagent restricted to
`read_file`, `list_dir`, `glob_search`, `grep`, `search_python`,
`inspect_python_module`.

**`explore_files`** — delegates a focused question about file/directory
layout or contents (e.g. "find the config files", "what's in this directory
tree") to a subagent restricted to `list_dir`, `glob_search`, `read_file`.

**`explore_logs`** — delegates a focused question about on-disk log file
contents (e.g. "find the first error in this log", "summarize what happened
around timestamp X") to a subagent restricted to just `read_file` and
`grep`. `run_shell` is deliberately excluded — even though it would be a
natural fit for something like `tail`ing a log — specifically to keep this
tool uniformly read-only, unlike `explore_codebase`/`explore_files` which
don't have a shell tool in their allowed set to begin with either, but where
the exclusion is worth calling out explicitly here since a log-exploration
task is the one most tempted to reach for a shell command.

New personas following this same shape can be defined at runtime by the
model itself via `register_agent_tool` (above), or by extending
`build_default_registry()` directly for ones that should always be present.

## write_todos

Lets the model maintain a structured task list for the session. Each call
**replaces the whole list** (not incremental deltas) — stored on
`Session.todos`, so it persists/exports/imports like everything else. Rejects
more than one `in_progress` item at a time.

- **Parameters:** `todos` (array of `{content: string, status: "pending" |
  "in_progress" | "completed"}`, required).
- **Permission:** not required.
- Requires `ctx.session` to be set (errors otherwise).
- **Flags dropped completed work.** Before applying the update, the old
  list's `completed` items are compared against the new list's content via
  `difflib.SequenceMatcher` (`_still_represented`,
  `src/pcli/tools/builtin/todo_tool.py:31`) — fuzzy text matching, threshold
  `_SIMILARITY_THRESHOLD = 0.45`, tolerant of ordinary rewording but not of
  wholesale replacement with an unrelated task. The update still applies
  either way (this is a nudge, not a block — a genuine restart is sometimes
  correct); but if any completed items have no reasonably-similar counterpart
  in the new list, the success output gets an appended note: `[pcli] Note: N
  previously completed item(s) no longer appear in this list (...). If you're
  deliberately restarting or changing approach, that's fine — record_decision
  helps track why. If not, the underlying work may already be done and
  doesn't need redoing.` Motivated by a real debugged session where the model
  silently replaced 4 verified-complete items with 3 new pending ones for a
  superficially similar task and redid already-finished work. Also called out
  in the system prompt's `# Tracking work` section (`src/pcli/agent/prompt.py`).

## record_decision

Logs one consequential decision the model made, with its reasoning, to the
session's decision log — stored on `Session.decisions`
(`src/pcli/session/models.py`'s `Decision` model: `decision`, `rationale`,
`created_at`). **Unlike `write_todos` above (which replaces the whole list
every call), each call here appends one entry** — nothing is ever mutated or
removed. A change of mind is logged as a new entry, not an edit of the old
one; don't assume the latest call reflects the full current state the way it
does for `write_todos`.

- **Parameters:** `decision` (string, required — what was decided, stated
  plainly), `rationale` (string, required — the reasoning, including
  supporting evidence when there is any).
- **Permission:** not required (`needs_permission=False`) — same stance as
  `write_todos`: pure session bookkeeping, no side effects.
- Requires `ctx.session` to be set (errors otherwise: "No session available to
  record decisions in.").
- Errors if either `decision` or `rationale` is missing/empty: "Both
  'decision' and 'rationale' are required." — with an appended suggestion
  restating the two fields directly: `decision` is what was decided, stated
  plainly; `rationale` is why, including supporting evidence when there is
  any (`src/pcli/tools/builtin/decision_tool.py:28`).
- The system prompt's `# Recording decisions` section
  (`src/pcli/agent/prompt.py`) instructs the model to reserve this for
  consequential decisions (not routine tool calls) and to include the
  evidence behind the decision in the rationale when there is any.
- Like `todos`, `decisions` is a plain `Session` field with no dedicated
  export/import handling — it rides along with the existing whole-session
  JSON dump/roundtrip (`session/export.py`).
- **TUI:** rendered as a distinct, always-visible message rather than the
  generic collapsed tool-result — see
  [`tui-guide.md`](tui-guide.md#decision-log).

## fetch_artifact

Retrieves (a windowed slice of) content previously archived by the automatic
truncation mechanism below. Supports two mutually exclusive modes: a
character-slice mode (`offset`/`limit`) and a grep-style search mode
(`pattern`/`context_lines`).

- **Parameters:** `artifact_id` (string, required), `offset` (integer,
  optional, default 0), `limit` (integer, optional, default 4000), `pattern`
  (string regex, optional), `context_lines` (integer, optional, default 2).
- **Permission:** not required.
- **Default mode (`pattern` omitted):** unchanged from before — returns the
  `[offset:offset+limit]` slice plus a footer noting the next offset to use
  if more remains.
- **Pattern mode (`pattern` given):** instead of a character slice, greps the
  archived content — finds every line matching the regex, keeps
  `context_lines` lines of surrounding context per match (like `grep -C`),
  and merges overlapping/adjacent context windows so nearby matches don't
  duplicate shared lines; separate (non-adjacent) blocks are joined with a
  `--` separator line, matching real `grep`'s own convention
  (`_grep_with_context`, `src/pcli/tools/builtin/artifact_tool.py:16`).
  `offset` is silently ignored in this mode — it only applies to the
  character-slice mode, and pattern mode has no meaningful use for a raw
  offset. `limit` is reused (not a new parameter) as an output-size cap,
  truncating with `[...output truncated to max_chars...]` if exceeded.
  Matches are capped at 50 (`_MAX_PATTERN_MATCHES`); output is prefixed with
  a header like "3 matching line(s):" (with "(showing first 50)" appended
  when the cap is hit), or "No lines matching '...' found in this artifact."
  if nothing matched.
- **Invalid regex** in `pattern` gets a clean error, not a crash, in the same
  style as the `grep` tool's own: "Invalid regex: ... [pcli] Suggestion: if
  you don't need regex features, escape the special character(s) or search
  for a plain substring instead." (`src/pcli/tools/builtin/artifact_tool.py:71`).
- **Unknown `artifact_id`** gets an appended suggestion to re-check the
  original tool result's "archived as artifact_id='art_...'" note rather
  than guessing, since ids aren't derivable any other way
  (`src/pcli/tools/builtin/artifact_tool.py:56`).
- The system prompt's `# Managing context` section recommends `pattern` over
  blind offset/limit pagination when you already know what you're looking
  for, and the `# Investigation scripts` section covers keeping script
  output narrow so a broad, truncated result is less likely in the first
  place (`src/pcli/agent/prompt.py`).

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

The TUI displays this exact same string (`ToolResultEvent.output` — i.e.
`chunk.output` in `ChatScreen._stream_response`, the *already*
archived/truncated value above, not `raw_output`) via
`MessageView.add_tool_result`, inside an expandable Collapsible — see
[`tui-guide.md`](tui-guide.md#tool-results). The one thing that changed is
that the view layer no longer applies a *second*, smaller truncation on top:
previously the TUI cut whatever it was given down to a fixed 2000 characters
for display; now the Collapsible shows all of `chunk.output` when expanded.
In practice that mostly matters for results between roughly 2000 chars and
`artifact_threshold_chars` (default 4000) — big enough that the old
view-layer cap used to hide the tail, but not big enough to be archived — for
truly large, archived output the human sees the same truncated
preview-plus-`fetch_artifact`-note the model does.

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

## Grounding conclusions in evidence

Prompt-level discipline only — no new tool, no new data model, no code-level
enforcement. The `# Grounding conclusions in evidence` section of
`BASE_SYSTEM_PROMPT` (`src/pcli/agent/prompt.py`), placed right after
`# Managing context`, instructs the model that when it states something as
fact — a root cause, "X causes Y", "the bug is in Z", "this is safe to do" —
it must be grounded in something actually observed *this session* (a file it
read, a command's output, a search result), not assumed from training
knowledge or pattern-matching on how the task looks, and to reference that
evidence directly: a `file_path:line`, the specific command/output that
showed it, or the tool call that confirmed it. If something hasn't been
verified and the model is inferring or guessing, it's told to say so plainly
rather than state it as settled. It's explicitly scoped to conclusions the
user will act on — routine narration doesn't need a citation for every
sentence.
