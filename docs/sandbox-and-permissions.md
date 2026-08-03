# Sandbox and permissions

Two independent layers gate what a tool call can actually do:

- **Sandbox** (`src/pcli/sandbox/`) — *where* a command runs (process/
  filesystem/network containment).
- **Permissions** (`src/pcli/permissions/`) — *whether* a tool call is
  allowed to run at all (hard guardrails, then remembered grants, then an
  interactive prompt).

They're independent and both apply: a guardrail can deny a command before it
ever reaches a sandbox, and an allowed command still only runs with whatever
containment the selected sandbox backend provides.

## Sandbox backends

All three backends implement the abstract `Sandbox` interface
(`sandbox/base.py`): `execute(ExecRequest) -> ExecResult` and `capabilities()
-> SandboxCapabilities` (network isolation / memory limit / CPU limit support
flags). `ExecRequest.command` can be an argv list (run directly, no shell) or
a string (run through the platform shell, needed for pipes/redirection in
free-form shell commands).

### Backend selection

`select_sandbox(backend_override, allowed_roots)` (`sandbox/selector.py`),
called once at `ChatScreen` startup:

- `"docker"` -> always `DockerSandbox`.
- `"subprocess"` -> always `RestrictedSubprocessSandbox`.
- `"none"` -> always `NullSandbox` (see below) — an explicit opt-out, never
  chosen automatically.
- `"auto"` (the default) -> probes `docker version --format
  '{{.Server.Version}}'` with a 1.5s timeout; uses `DockerSandbox` if it
  succeeds, else falls back to `RestrictedSubprocessSandbox`. `auto` never
  selects `NullSandbox` — that requires an explicit `sandbox_backend = "none"`.
- Any other value (not `"auto"`/`""`/`"docker"`/`"subprocess"`/`"none"`)
  raises `ValueError: Unknown sandbox_backend`.

The resolved backend name (`docker`, `subprocess`, or `none`) is shown in the
status bar so you always know the isolation level in effect.

### DockerSandbox

One-shot `docker run --rm -i` per invocation (shells out to the `docker` CLI,
no Python `docker` SDK dependency). Defaults: image `python:3.12-slim`,
`--memory 512m`, `--cpus 1`, `--pids-limit 256`, network `none` unless
`ExecRequest.network=True` (then `bridge`). Working directory is bind-mounted
read-write at `/workspace`. Supports network isolation, memory limits, and
CPU limits (`SandboxCapabilities` all `True`). Output is truncated at
2,000,000 bytes (`max_output_bytes`); a timeout kills the container and
returns `timed_out=True`.

### RestrictedSubprocessSandbox

Cross-platform fallback: process + filesystem containment via a cwd jail,
scrubbed environment, resource limits where the OS supports them, and a
wall-clock timeout as the universal safety net.

- **cwd jail:** `_validate_cwd` resolves the request's cwd and requires it be
  one of (or under) `allowed_roots`; otherwise raises `SandboxSecurityError`.
- **Scrubbed env:** only an explicit allowlist of names passes through from
  the real environment (`PATH`, `HOME`, `USERPROFILE`, `SYSTEMROOT`,
  `SYSTEMDRIVE`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `PATHEXT`, `COMSPEC`,
  `PYTHONIOENCODING`); any extra env vars passed in are added *unless* their
  name contains `_KEY`, `_TOKEN`, `_SECRET`, `_PASSWORD`, `_CREDENTIAL`, or
  `_AUTH`.
- **Resource limits (POSIX only):** `RLIMIT_CPU` (default 30s) and
  `RLIMIT_AS` (default 512MB) via a `preexec_fn`. Windows has no `resource`
  module equivalent — there, the wall-clock timeout plus
  `psutil`-based process-tree kill on timeout is the actual safety net.
- **No network isolation on any platform** — `capabilities()` always reports
  `supports_network_isolation=False`. Guardrails/permission prompts should
  treat network-sensitive calls as always-ask when this backend is active.
- Output truncated at 2,000,000 bytes, same as Docker.

`pydiscovery`'s `inspect_python_module`/`call_python` tools and the toolbox's
version/`--help` probes always use a dedicated
`RestrictedSubprocessSandbox(allowed_roots=[cwd])` directly — never
`ctx.sandbox` — because they need the *host* interpreter and its installed
packages, which a bare Docker image wouldn't have.

### NullSandbox

Selected only via explicit `sandbox_backend = "none"`
(`src/pcli/sandbox/null_backend.py`, `name = "none"`) — runs the command
directly with `asyncio.create_subprocess_exec`/`_shell` and *none* of
`RestrictedSubprocessSandbox`'s containment: no cwd jail (runs at
`request.cwd` unchecked), no env scrubbing (full inherited `os.environ` plus
any extra vars the caller passed, layered on top — "no sandbox" means no
restriction, not no environment), and no resource limits.
`capabilities()` reports `supports_network_isolation=False`,
`supports_memory_limit=False`, `supports_cpu_limit=False` — nothing is
contained. Output is still truncated at 2,000,000 bytes
(`max_output_bytes` constructor default) and a timeout still kills the
process and returns `timed_out=True`, same as the other backends.

This is an intentional, documented opt-out for trusted environments only
(e.g. pcli already running inside its own disposable container/VM) — never
pick it against an untrusted project. Guardrails and the permission manager
still gate every tool call exactly as usual regardless of which sandbox
backend is active; `"none"` only removes the *execution* containment layer
underneath that gate, not the gate itself.

## Guardrails

`GuardrailsConfig` (`src/pcli/permissions/guardrails.py`) is a hard,
config-driven, non-negotiable check: a violation is an automatic deny that
**never reaches the permission-prompt UI**, evaluated before the
remember/ask flow. It's a heuristic safety net (substring/wildcard matching
on the raw command string, not a shell parser) meant as defense-in-depth
alongside the sandbox, not a substitute for it.

Loaded from `guardrails_file()` (`config_dir()/guardrails.toml`); if missing,
the file is created with these built-in defaults:

```toml
[shell]
denylist = ["rm -rf /", "rm -rf ~", "mkfs*", ":(){ :|:& };:", "dd if=/dev/zero", "> /dev/sda"]

[fs]
allowed_roots = ["."]
deny_paths = ["~/.ssh", "~/.aws", "~/.config/pcli"]

[limits]
max_output_bytes = 2000000
max_tool_calls_per_turn = 25
max_tool_calls_per_minute = 60

[python]
module_denylist = ["os", "sys", "subprocess", "ctypes", "shutil", "socket", "importlib", "multiprocessing", "threading", "pty"]
```

Three checks, each tied to a specific tool argument via `ToolSpec`'s
`guardrail_command_arg` / `guardrail_path_arg` / `guardrail_python_module_arg`
(see [`tools.md`](tools.md) for which tools set which):

- **`evaluate_command(command)`** — denylist patterns without `*`/`?` match
  as a case-insensitive substring of the command; patterns with `*`/`?` match
  via shell-style wildcards anywhere in the command.
- **`evaluate_path(path)`** — resolves the path; denied if it's within (or
  equal to) any `deny_paths` entry; allowed only if it's within (or equal to)
  one of `allowed_roots`; otherwise denied ("outside all allowed roots").
- **`evaluate_python_module(qualified_name)`** — denied if the *top-level*
  module name is in `python_module_denylist` (these overlap with the
  dedicated fs/shell tools, so letting the LLM reach them indirectly via
  arbitrary Python calls would be a redundant, higher-risk escape hatch).

All three `[limits]` values are actively enforced, each at a different layer:

- **`max_output_bytes`** — enforced in `AgentLoop._dispatch_tool_call`
  (`src/pcli/agent/loop.py`) as a global backstop applied to *every* tool's
  raw output, regardless of whether that tool has its own smaller internal
  cap (e.g. `run_shell`'s 100,000-char `_MAX_OUTPUT_CHARS`, each sandbox
  backend's own 2,000,000-byte cap). It runs after the handler returns and
  before the separate artifact-archiving truncation
  ([`tools.md`](tools.md#artifact-archiving)) — so a huge result is first
  clipped to `max_output_bytes`, then (if still over
  `artifact_threshold_chars`) archived and previewed. When it truncates, it
  appends: `\n[...output truncated to the guardrail limit of {max_output_bytes}
  chars...]`.
- **`max_tool_calls_per_turn`** — enforced in `AgentLoop.run_turn`: a counter
  of tool calls actually dispatched is tracked across the *whole turn* (not
  reset per LLM round-trip/iteration). Once the counter reaches the limit,
  every remaining `tool_call` in the current batch still gets a `"Denied:
  reached the guardrail limit of {N} tool call(s) for this turn."` tool-role
  response each (every `tool_call_id` from the model must be answered per the
  chat-completions protocol) rather than being executed; the loop then
  appends a `[pcli] Reached the guardrail limit of {N} tool call(s) for this
  turn.` note and stops — no further LLM round-trip happens that turn.
- **`max_tool_calls_per_minute`** — enforced in
  `PermissionManager._within_rate_limit` (`src/pcli/permissions/manager.py`)
  as a sliding 60-second window (a `deque` of call timestamps, pruned against
  `now - 60.0`), checked as the very first thing in `check()` — before the
  guardrail command/path/module checks even run. It's shared across every
  call gated by that one `PermissionManager` instance, which notably includes
  a subagent's tool calls too, since a subagent is handed the same
  `PermissionManager` instance as its parent. A limit of `0` or less disables
  the check entirely (always allowed).

**Local-API mode disables both `max_tool_calls_per_turn` and
`max_tool_calls_per_minute`, but only for the paired gateway.** When
`Settings.is_local_api()` is true (see `--local-api` in
[`configuration.md`](configuration.md#local-api-mode)), `ChatScreen.__init__`
builds its `PermissionManager` from a `GuardrailsConfig.model_copy()` with
both limits forced to `0` — i.e. unlimited, per the "disables the check
entirely" behavior above and the equivalent `<= 0` check in
`AgentLoop.run_turn` for `max_tool_calls_per_turn`. This is the *only* thing
that disables these two guardrail limits; the security guardrails just above
(`evaluate_command`, `evaluate_path`, `evaluate_python_module`) and
`max_output_bytes` are untouched by local-api mode and still enforced exactly
as normal.

## Permission manager

`PermissionManager.check(...)` (`src/pcli/permissions/manager.py`) is the
full decision flow for one tool call, run from `AgentLoop._dispatch_tool_call`:

1. Guardrails first — command/path/python-module checks above. Any violation
   is an immediate `"deny"`, regardless of everything else.
2. If the tool doesn't need permission (`default_allow=True`, i.e.
   `ToolSpec.needs_permission=False`), allow.
3. Otherwise, check `PermissionPolicy` for an existing remembered grant for
   this tool name (session or always-allow) — if found, use it.
4. Otherwise, call the `ask` callback (in the TUI, this pushes
   `PermissionPromptModal` and awaits the user's choice — see
   [`tui-guide.md`](tui-guide.md)). If no `ask` callback is available (e.g.
   headless), **fail closed**: deny.
5. If the user's choice includes a remember scope other than `"once"`, it's
   recorded via `PermissionPolicy.remember(...)`.

### Grant scopes (`PermissionPolicy`, `src/pcli/permissions/policy.py`)

- **`once`** — not remembered at all.
- **`session`** — kept in an in-memory list for the life of the process only.
- **`always`** — appended to the in-memory list *and* persisted to
  `permissions_file()` (`config_dir()/permissions.json`), so future pcli runs
  skip the prompt for that tool.

Grants are matched by tool name (optionally an `argument_pattern`, though
nothing in the codebase currently sets one — all current grants are
name-only, so allowing a tool once for a session/always allows it for *any*
arguments for the remainder of that scope). Session grants are checked before
always grants, but since both single-branch on tool name with no pattern in
practice, the order rarely matters. Persistence uses an atomic write (temp
file + `os.replace`).

`PermissionManager.check(...)` also takes an optional `session: Session |
None = None` kwarg. Whenever the user's choice remembers a `"session"` or
`"always"` grant via `self.policy.remember(...)`, a matching
`PermissionGrant` (`src/pcli/session/models.py`) is now also appended to
`session.permission_grants` if a session was passed —
`AgentLoop._dispatch_tool_call` builds `ctx` (and so `ctx.session`) *before*
calling `check()` specifically so this can happen, then passes
`session=ctx.session`.

This is a historical/audit record that travels with the session, not a
second enforcement path: "always" grants are still enforced solely via
`self.policy`/`permissions.json`, and `permission_grants` is deliberately
**not** auto-re-applied into a fresh `PermissionPolicy`/`permissions.json` on
import — doing so would duplicate-write "always" grants that are already
persisted separately. See
[`sessions-and-cost.md`](sessions-and-cost.md) for the export/import
`--restore-grants` behavior this feeds.
