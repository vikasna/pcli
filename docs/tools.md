# Built-in tools

`build_default_registry()` (`src/pcli/tools/registry.py`) registers these
tools into every session's `ToolRegistry`. Toolbox-discovered tools
(kubectl, SGE, etc.) are merged in separately — see
[`toolbox-plugins.md`](toolbox-plugins.md).

Every tool call goes through `AgentLoop._dispatch_tool_call`
(`src/pcli/agent/loop.py`): an identical-repeat check first (the same tool
name plus the same purpose-stripped arguments, three times in a row, is
blocked outright — see [`sandbox-and-permissions.md`](sandbox-and-permissions.md#identical-tool-call-repeat-guard)),
then JSON-schema validation of arguments, then
`PermissionManager.check_with_reason` (guardrails first, then remembered
grants/ask-the-user — see [`sandbox-and-permissions.md`](sandbox-and-permissions.md)),
then the handler runs, then the result passes through the artifact-archiving
choke point described below before going back to the model. A denial is
reported back to the model as `"Permission denied: <reason>."` (or the bare
`"Permission denied."` when there's no specific reason), so it has something
concrete to diagnose — see the "Surfacing the reason back to the model"
section of [`sandbox-and-permissions.md`](sandbox-and-permissions.md#permission-manager)
for the exact reason strings.

Each entry lists: what it does, its JSON-schema parameters, whether it
prompts for permission (`needs_permission`), and any guardrail hook.

## The `purpose` argument

Every tool's advertised schema gets one extra property beyond what's listed
per-tool below: an optional `purpose` string, injected by
`ToolSpec.to_openai_tool()` (`src/pcli/tools/base.py`) on a shallow copy of
`self.parameters` — the real `parameters` used for schema validation is never
mutated, so this is purely advertised to the model, not a real tool argument.
It's meant to be a short, one-sentence reason the model is calling the tool
right now, e.g. `"checking whether pdftotext is installed"`; the system
prompt's `# Managing context` section (`src/pcli/agent/prompt.py`) encourages
the model to always include it.

`AgentLoop._dispatch_tool_call` (`src/pcli/agent/loop.py`) pops `"purpose"`
out of the parsed arguments *before* JSON-schema validation and before the
tool handler ever runs, on a copy — the original arguments JSON string as the
model sent it (still containing `"purpose"`) is left untouched in the
persisted session, which is what lets later code re-extract it. This matters
for two things: it keeps a toolbox-synthesized tool's auto-generated
CLI-flag-builder (see [`toolbox-plugins.md`](toolbox-plugins.md)) from
treating `purpose` as a bogus flag, and it's shown on its own italic line
above the call's formatted arguments in the TUI (see
[`tui-guide.md`](tui-guide.md#tool-calls-and-results)) and resurfaces later
in a pruned tool result's placeholder (see [Artifact
archiving](#artifact-archiving) below and
[`tui-guide.md`](tui-guide.md#tool-result-pruning)).

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

## download_file

A structured, cross-platform alternative to shelling out to
`curl`/`wget`/`Invoke-WebRequest` via `run_shell` — added after a real
debugged session where a model got stuck retrying a bash heredoc
(`python << 'EOF' ... EOF`) that fails outright on a Windows/cmd.exe shell,
compounded by a dead download URL it kept retrying unchanged. Built on
`httpx` (already a pcli dependency, used throughout `llm/client.py`, so no
new dependency was added), not `requests`.

Streams the response body to disk (`response.aiter_bytes()`, never loading
the whole file into memory) and follows redirects automatically
(`httpx.AsyncClient(follow_redirects=True, ...)`).

- **Parameters:** `url` (string, required), `path` (string, required —
  destination to save to, relative to the working directory or absolute;
  parent directories are created automatically via `mkdir(parents=True,
  exist_ok=True)`), `timeout_s` (number, optional, default 30).
- **Permission:** required. `risk_description`: "Downloads content from a
  URL and writes it to disk." — same stance as `write_file`, since it both
  makes a network request and writes to disk. Not `plan_mode_safe`.
- **Guardrail:** `path` checked against the fs allow/deny lists
  (`guardrail_path_arg="path"`), same mechanism as every other path-taking
  tool.
- **500 MB safety cap** (`_MAX_DOWNLOAD_BYTES`): if the streamed response
  exceeds this while writing, the download is aborted and the partial file
  on disk is deleted; the error output suggests `run_shell_background` with
  a dedicated download command if the file is genuinely expected to be that
  large.
- **Failure handling, each with its own `[pcli] Suggestion: ...` appended
  to the error output** (`src/pcli/tools/builtin/network_tools.py`):
  - **HTTP status >= 400:** checked before the destination file is ever
    opened for writing, so there's no partial file to clean up — the tool
    just returns the error. Suggestion: re-verify the URL is correct and
    still reachable (e.g. a moved/renamed dataset or release asset) —
    retrying the identical URL won't fix a 404/403/etc.
  - **Timeout** (`httpx.TimeoutException`) or **connection error** (any
    other `httpx.HTTPError`, once streaming has started): the partial file
    already written to `path` is deleted
    (`resolved.unlink(missing_ok=True)`) before returning, so a failed
    download never leaves a truncated file behind. Suggestion for a timeout: raise `timeout_s`
    for a large file or slow connection, or use `run_shell_background` if
    it may take several minutes. Suggestion for a connection error: check
    the URL is reachable and correctly formed, and that network access is
    actually available from this environment.
- The system prompt's `# Network access` section (`src/pcli/agent/prompt.py`)
  tells the model to prefer `download_file`, `web_fetch`, and `web_search`
  over a shell-based download/fetch (`curl`, `wget`, `Invoke-WebRequest`) for
  anything involving the network: each is a single, cross-platform tool call
  with no shell syntax to get wrong, reporting a clear HTTP status/error
  instead of a raw stderr blob to parse — see [`web_fetch`](#web_fetch) and
  [`web_search`](#web_search) above for the other two.

## web_fetch

Fetches a URL and returns its readable text content. Built on `httpx` (no new
dependency) plus a small stdlib `html.parser.HTMLParser`-based extractor
(`_TextExtractor`, `src/pcli/tools/builtin/web_tools.py`) — drops
`script`/`style`/`noscript` content, inserts a newline at block-level tag
boundaries (`p`, `br`, `div`, `li`, `tr`, `h1`-`h6`, `section`, `article`), and
keeps everything else. This is deliberately best-effort, not a
readability-style boilerplate remover — nav/footer/ad text stays in.
Along with `web_search` below, this is one of pcli's only two tools with real
internet access; every other tool is local (filesystem, shell, or the
configured LLM gateway itself).

- **Parameters:** `url` (string, required), `max_chars` (integer, optional,
  default 8,000 — output longer than this is truncated with an appended
  `[...truncated to N chars...]` note).
- **Permission:** required. `risk_description`: "Fetches content from a URL
  over the network." `plan_mode_safe=True` — it's read-only (no local
  mutation), so it stays available while [plan mode](tui-guide.md#plan-mode)
  is active.
- **Content-type handling:** an HTML response (`"html"` in the `content-type`
  header) is run through `_html_to_text`; `text/*` or `application/json` is
  returned as-is; anything else (binary content types) is rejected with a
  clean error naming the content type and suggesting `download_file` instead
  if the content is actually needed on disk.
- **5,000,000-byte safety cap** (`_MAX_FETCH_BYTES`) on the raw response body
  — if exceeded, the fetch is rejected (not truncated) with a suggestion that
  this looks like a large binary/media file and to use `download_file`
  instead if it's genuinely needed on disk.
- **Failure handling, each with an appended `[pcli] Suggestion: ...`**
  (`src/pcli/tools/builtin/web_tools.py`): a timeout (20s,
  `_DEFAULT_TIMEOUT_S`) suggests retrying or using another source; any other
  `httpx.HTTPError` suggests checking the URL and that network access is
  actually available; an HTTP status `>= 400` suggests double-checking the
  URL is correct and still live, since retrying an identical 404/403 won't
  help.

## web_search

Searches the web and returns a short list of results (title, URL, snippet).
Along with `web_fetch` above, this is one of pcli's only two tools with real
internet access.

Uses the [Brave Search API](https://api.search.brave.com/res/v1/web/search)
when `Settings.brave_search_api_key` is configured (non-empty) — see
[`configuration.md`](configuration.md#settings-fields). Otherwise it falls
back to scraping DuckDuckGo's server-rendered HTML results page
(`https://html.duckduckgo.com/html/`, POST with a hardcoded browser-like
`User-Agent` — confirmed necessary directly against a real response: httpx's
default UA gets a soft-blocked empty/homepage response instead of results).
This fallback needs no API key or setup and works out of the box, but is
explicitly documented in the source as best-effort and fragile — it depends
on DuckDuckGo's current HTML markup (organic results are parsed as
`<div class="web-result">` containing `<a class="result__a">` for
title+URL and `<a class="result__snippet">` for the snippet; `result--ad`
divs are skipped) and could break if that markup changes. Treat results from
either backend as a starting point, not verified fact — follow up with
`web_fetch` on whatever looks promising before answering from a
title/snippet alone.

The tool description also gives the model query-construction guidance,
confirmed directly against the DuckDuckGo fallback rather than assumed: use
short, keyword-based queries (roughly 2-6 words) instead of a full question
or sentence — `python asyncio cancel task` beats `how do I cancel a task in
python asyncio`. Quote an exact phrase (an error message, a function name, a
title) when it needs to match verbatim — quoting is reliable on both
backends. Avoid `site:`/`filetype:`/`-exclusion`/`OR` search operators on
the DuckDuckGo fallback: they were directly tested and confirmed broken — a
query using any of them comes back HTTP 202 with zero results (a soft
block), not just a degraded or literal-text match. Those operators only work
when a real search API (Brave) is configured. If a specific site is needed,
search normally and pick the matching result, or `web_fetch` a known URL
directly instead.

- **Parameters:** `query` (string, required), `max_results` (integer,
  optional, default 5).
- **Permission:** required. `risk_description`: "Sends a search query to a
  third-party service over the network." `plan_mode_safe=True`, same reasoning
  as `web_fetch` — read-only, no local mutation.
- **Failure handling** (`src/pcli/tools/builtin/web_tools.py`): a timeout
  (20s) suggests retrying or narrowing the query; any other `httpx.HTTPError`
  suggests checking network access, and — if this keeps failing — that the
  no-API-key DuckDuckGo fallback may be getting blocked, pointing at
  configuring `brave_search_api_key` for a real, supported search API
  instead.

## diff_files

Compares two text files and returns a unified diff (like `diff -u`), built on
stdlib `difflib` — a structured, cross-platform alternative to shelling out to
the `diff` CLI, which doesn't ship with Windows (same motivation as
`download_file` above replacing `curl`/`wget`).

Reads both files as UTF-8 (`errors="replace"`), splits into lines
(`splitlines(keepends=True)`), and runs `difflib.unified_diff(lines_a,
lines_b, fromfile=path_a, tofile=path_b, n=context_lines)`. If the files are
identical, returns `"No differences."` instead of an empty diff.

- **Parameters:** `path_a` (string, required — the "before" file), `path_b`
  (string, required — the "after" file), `context_lines` (integer, optional,
  default 3 — lines of unchanged context around each change).
- **Permission:** not required (`needs_permission=False`) — read-only.
  `plan_mode_safe=True`, so it stays available while
  [plan mode](tui-guide.md#plan-mode) is active.
- **Guardrail:** only `path_a` goes through the automatic
  `guardrail_path_arg` check that `AgentLoop` applies per tool call, since it
  only threads one path argument per call; `path_b` is checked by hand inside
  the handler against the same fs allow/deny lists
  (`src/pcli/tools/builtin/diff_tools.py`), so both sides are guarded either
  way.
- **Either file missing/not-a-file** gets a clean error (not a crash) naming
  which of `path_a`/`path_b` was the problem, with an appended suggestion to
  check the path with `list_dir` or `glob_search` before diffing.

## apply_patch

Applies a unified diff (as produced by `diff_files` above or `git diff`) to a
file, modifying it in place. Like `diff_files`, this exists so pcli never has
to shell out to the `patch` CLI, which also doesn't ship with Windows.
Python's stdlib has no patch-application function — only `difflib` for
diffing — so this is a small hand-rolled unified-diff parser/applier
(`_parse_hunks`/`_apply_hunks` in `src/pcli/tools/builtin/diff_tools.py`).

The system prompt's `# Editing files` section (`src/pcli/agent/prompt.py`)
tells the model to prefer `apply_patch` over several separate `edit_file`
calls for a multi-hunk change, and to fall back to `edit_file` for the
specific change if `apply_patch` fails.

- **Parameters:** `path` (string, required — file to patch), `patch` (string,
  required — unified diff text containing `@@ ... @@` hunk headers).
- **Permission:** required. `risk_description`: "Modifies a file on disk by
  applying a patch." Not `plan_mode_safe` (it writes to disk).
- **Guardrail:** `path` checked against the fs allow/deny lists
  (`guardrail_path_arg="path"`), same mechanism as `write_file`/`edit_file`.
- **Requires an exact match — no fuzzy offset matching.** Unlike the real
  `patch` CLI, which can shift a hunk to a nearby line when the surrounding
  context has drifted slightly, this applier requires the file's current
  content to exactly match the patch's context and removed lines at the
  hunk's stated line number. This is the main thing to know before reaching
  for this tool: if the target file has changed at all since the patch was
  generated (even a single unrelated line), the hunk will not "just work" at
  an offset — it fails cleanly instead.
- **`path` doesn't exist** gets a clean error pointing at `write_file` to
  create it first, rather than trying to patch a file that isn't there.
- **Patch-application failures** each get a specific error plus an appended
  `[pcli] Suggestion: ...` (`src/pcli/tools/builtin/diff_tools.py`):
  - No `@@ ... @@` hunk headers found in `patch` at all.
  - An unrecognized line inside a hunk body (not a context/removed/added
    line).
  - A hunk's line ranges overlap the previous hunk, or a hunk starts past the
    end of the file.
  - A context or removed line doesn't exactly match the file's actual content
    at that line — reported as e.g. `Context mismatch at line N: patch
    expects 'X', file has 'Y'`.
  - In every case, the suggestion points at the same recovery path: re-read
    the file with `read_file`, regenerate the diff against its current
    content with `diff_files`, or fall back to `edit_file` for a single
    targeted change instead.

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
tracking via `ToolResult.extra_usage`, tagged `source="subagent"` on the
recorded `TurnCost` so it's counted toward the session total but not mistaken
for the main conversation's own size (see
[`sessions-and-cost.md`](sessions-and-cost.md#turncostsource)).

The nested `AgentLoop` also **inherits the parent's current dynamic
response-length cap and sampling temperature** — `max_response_tokens=
ctx.max_response_tokens, temperature=ctx.temperature` — rather than always
running uncapped at the gateway's default temperature. Both values track
whatever `/max-response-tokens` and `/temperature` currently have configured
on the parent session; see
[`configuration.md`](configuration.md#dynamic-response-cap).

**Subagents can never spawn further subagents** — `spawn_subagent` is always
filtered out of the tool registry a subagent runs with (by name, in
`_spawn_subagent`), so nesting is capped at exactly one level by construction.

- **Parameters:** `task` (string, required), `allowed_tools` (array of
  strings, optional — restricts the subagent's toolset), `max_iterations`
  (integer, optional).
- **Iteration cap:** `min(requested_max_iterations, ctx.subagent_max_iterations)`
  if `max_iterations` is given, else `ctx.subagent_max_iterations` itself —
  `subagent_max_iterations` defaults to 30 and is configurable via
  `PCLI_SUBAGENT_MAX_ITERATIONS`/`config.toml` (see
  [`configuration.md`](configuration.md#settings-fields)). Requesting a
  larger `max_iterations` than this never raises the effective cap, only
  lowers it — there's always a hard ceiling regardless of what the model
  asks for, and it's enforced even in
  [local-api mode](configuration.md#local-api-mode), unlike
  `max_tool_iterations` itself.
- **Permission:** required. `risk_description`: "Spawns a subagent that can
  call tools (including sandboxed ones) on its own."
- **Live progress:** while running, reports task/tool-call-count/last-tool to
  `ActivityTracker`, which the TUI's status bar renders as a second line, and
  the full call history (name + arguments) plus any pending
  `ask_user_question` to `/subagent` (see [`tui-guide.md`](tui-guide.md) and
  [`tui-guide.md#slash-commands`](tui-guide.md#slash-commands)).
- **Failures get a suggestion, not just the raw error**
  (`src/pcli/tools/builtin/subagent_tool.py:39`): if subagents aren't
  available in this context (no gateway/tools/permissions configured), the
  suggestion is to handle the task directly instead of delegating; if the
  subagent itself raised an exception, the suggestion is to retry with a
  narrower task description or handle it directly.
- **A subagent that gets cut off before finishing is reported as a failure,
  not a success.** If the subagent's own turn hits the iteration cap above
  (or the `max_tool_calls_per_turn` guardrail) instead of the model
  concluding on its own — `TurnCompleteEvent.terminated_early`,
  `src/pcli/agent/loop.py` — the `ToolResult` returned to the parent is
  marked `is_error=True` and its output is prefixed with "SUBAGENT DID NOT
  FINISH — it hit its tool-call iteration limit (N) before completing the
  task below. Treat this as INCOMPLETE: do not report the task as done, and
  verify what (if anything) was actually produced ... before telling the
  user it succeeded." This exists specifically so a weak model can't
  fabricate a success report over a subagent whose task genuinely wasn't
  finished; the message also points the model at raising
  `subagent_max_iterations` or splitting the work into a narrower follow-up
  task.

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
- Like `spawn_subagent`, each nested `AgentLoop` inherits the parent's
  current `max_response_tokens`/`temperature` (`ctx.max_response_tokens`/
  `ctx.temperature` in `make_agent_tool`) and its own usage is recorded with
  `source="subagent"` — see the `spawn_subagent` entry above.
- Same `ctx.subagent_max_iterations` cap as `spawn_subagent` (no
  per-call `max_iterations` argument to tighten it further here, since these
  take only `query`), and the same "DID NOT FINISH" treatment if it's hit:
  `is_error=True` with a `'<name>' DID NOT FINISH — it hit its tool-call
  iteration limit (N) before completing.` prefix
  (`src/pcli/tools/agent_tools.py`'s `make_agent_tool`) — see the
  `spawn_subagent` entry above for the full reasoning.

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

## write_documentation, verify_computation, deep_research, data_analysis

Four more ready-made agent tools registered by default in
`build_default_registry()`, built on the same `make_agent_tool` mechanism as
`explore_codebase`/`explore_files`/`explore_logs` above. Same shape: a single
`query` argument, `needs_permission=True`, the same live-progress reporting
to `ActivityTracker`, and the same `ctx.subagent_max_iterations` cap plus
"DID NOT FINISH" failure treatment if it's hit (see the `explore_codebase`
entry above). Unlike the three `explore_*` tools, these are *not* uniformly
read-only — the `plan_mode_safe` value is chosen per tool based on whether
its allowed-tool list is itself entirely read-only.

**`write_documentation`** — delegates writing or updating documentation for a
code change to a subagent that reads the existing docs' style and the actual
code before writing, so the result matches the project's conventions instead
of a generic template. Key persona instruction: read the existing
documentation and the actual code being documented before writing a single
line, verifying claims (signatures, flags, paths, defaults) against the real
code rather than paraphrasing what it was told changed. Allowed tools:
`read_file`, `list_dir`, `glob_search`, `grep`, `write_file`, `edit_file`.
`plan_mode_safe=False` — it writes to disk, so the whole tool is unavailable
while [plan mode](tui-guide.md#plan-mode) is active, same as `write_file`
itself.

**`verify_computation`** — delegates checking a calculation, algorithm, or
numeric claim to a subagent that verifies it by writing and running code
rather than reasoning about it in text. Key persona instruction: never trust
mental arithmetic or reasoning-in-text as a final answer — write and run code
to compute or check every numeric claim, and state a plain verdict (correct,
incorrect, or partially correct) showing the computed value next to the
claimed one. Allowed tools: `read_file`, `write_file`, `call_python`,
`run_shell`. `plan_mode_safe=False`.

**`deep_research`** — delegates a question that needs thorough investigation
— local (files, logs, code, git history via `run_shell`) and/or web
(`web_search`, `web_fetch`) — to a subagent that cross-checks claims against
multiple sources and cites them rather than giving a single-source shallow
answer. Key persona instruction: start with short, broad queries and
progressively narrow rather than committing to one angle immediately; never
state a fact found in only one place as settled — note it as single-sourced
or find a second independent source, and say so explicitly if sources
disagree. Allowed tools: `read_file`, `list_dir`, `glob_search`, `grep`,
`search_python`, `inspect_python_module`, `run_shell`, `web_search`,
`web_fetch`. `plan_mode_safe=True` — even though `run_shell` is in its
allowed list, the plan-mode filtering in `make_agent_tool` (see
`register_agent_tool` above) strips non-`plan_mode_safe` tools like
`run_shell` from its *effective* set while plan mode is active, so the tool
itself stays callable and still useful (read-only exploration plus web
tools) rather than being blocked outright.

**`data_analysis`** — delegates exploring or analyzing a local dataset
(CSV/JSON/etc.) to a subagent that computes real statistics via code rather
than guessing at what's in the data, and flags data-quality issues as a
matter of course. Key persona instruction: before characterizing the data in
any way, load and inspect it programmatically (shape, dtypes, null rates,
real computed aggregates) rather than guessing from a glance at a few rows,
and state assumptions and data-quality issues (nulls, outliers, duplicates,
inconsistent types, unexpected cardinality) even if not specifically asked.
Allowed tools: `read_file`, `list_dir`, `glob_search`, `call_python`,
`run_shell`, `write_file`. `plan_mode_safe=False`.

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

## ask_user_question

Lets the model pause a turn to ask the user something directly — a genuine
ambiguity it can't safely resolve itself, or a choice between approaches only
they can make — rather than guessing or leaving a question in plain response
text and hoping the user notices and replies in their next message. Mirrors
opencode's "ask" tool. The system prompt's `# Asking questions` section
(`src/pcli/agent/prompt.py`) gives the model guidance on when this is
actually warranted (genuine ambiguity, a choice only the user can make,
unclear scope before a destructive action) versus when it's not (anything
inferable, verifiable, or reasonable to assume-and-state) — it's explicitly
framed as not a first resort, since asking blocks the whole turn and costs
the user a context switch.

- **Parameters:** `question` (string, required), `options` (array of
  strings, optional — a short list of suggested answers, shown as quick
  choices; the user can still type something else regardless of whether
  options are given).
- **Permission:** not required (`needs_permission=False`) — it's read-only,
  no side effects. `plan_mode_safe=True`, so it stays available while
  [plan mode](tui-guide.md#plan-mode) is active, since asking a clarifying
  question is itself investigation, not a change.
- **No UI available** (`ctx.ask_question is None` — e.g. headless/local-api
  usage with no TUI to show a modal in): returns a clean error instead of
  blocking forever, telling the model to state its assumption plainly and
  proceed instead (`src/pcli/tools/builtin/ask_tool.py`).
- **TUI:** implemented by `AskQuestionModal`
  (`src/pcli/tui/screens/ask_question_modal.py`) — see
  [`tui-guide.md`](tui-guide.md#ask-question-modal) for how it renders and
  blocks the turn until answered.
- **Called from inside a subagent**, the question text is automatically
  prefixed with what that subagent has been working on (its task and the 5
  most recent tool call names, e.g. `[This question is from a subagent
  working on: "..." — N tool call(s) so far, most recent: ...]`) before
  being handed to `ctx.ask_question` (`_ask_user_question`,
  `src/pcli/tools/builtin/ask_tool.py`) — otherwise the user would see a
  bare, context-free question, since a subagent's own intermediate work
  never enters the main conversation (see the `spawn_subagent` entry above).
  This also records the question on `ActivityTracker.subagent.pending_question`
  for the duration of the ask, so [`/subagent`](tui-guide.md#slash-commands)
  can show it while it's waiting.

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
[`tui-guide.md`](tui-guide.md#tool-calls-and-results). The one thing that
changed is
that the view layer no longer applies a *second*, smaller truncation on top:
previously the TUI cut whatever it was given down to a fixed 2000 characters
for display; now the Collapsible shows all of `chunk.output` when expanded.
In practice that mostly matters for results between roughly 2000 chars and
`artifact_threshold_chars` (default 4000) — big enough that the old
view-layer cap used to hide the tail, but not big enough to be archived — for
truly large, archived output the human sees the same truncated
preview-plus-`fetch_artifact`-note the model does.

A second, later consumer of this exact same archive-then-retrieve pattern is
tool-result *pruning* (`agent/context_pruning.py`): once a tool result has
aged out of the conversation's recent window, its content (already possibly
this section's own truncated preview, if it was large enough to be archived
here first) is itself archived via `ArtifactStore.put()` and replaced with a
short placeholder referencing a `fetch_artifact` call — the same idea as
above, just applied on a delay based on turn age rather than immediately on
result size. See [`tui-guide.md`](tui-guide.md#tool-result-pruning) for the
full mechanism and [`configuration.md`](configuration.md#tool-result-pruning)
for the settings that control it.

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
  boundaries (`turn_boundaries`) so an assistant `tool_calls` message is
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
