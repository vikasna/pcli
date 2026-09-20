# Development

## Setup

```
pip install -e ".[dev,docker,win,browser]"
pcli
```

- `dev` (`pyproject.toml`) pulls in `pytest`, `pytest-asyncio`, `respx`,
  `ruff`, `mypy`.
- `win` pulls in `pywin32` (only installs on Windows, via `sys_platform ==
  'win32'` marker).
- `docker` is intentionally empty — the Docker sandbox backend
  (`DockerSandbox`) shells out to the `docker` CLI binary directly rather than
  depending on a Python `docker` SDK, so there's nothing to install. The extra
  exists purely so `pip install -e ".[dev,docker,win]"` (as documented in
  `README.md`) is valid instead of erroring on an undefined extra name.
- `browser` pulls in `playwright>=1.40`, for the `browser_*` tools
  (`tools/builtin/browser_tool.py`). Unlike `docker`, this one isn't
  self-sufficient once installed: Playwright's own browser binary is a
  separate download, fetched with a one-time `playwright install chromium`
  after this. See [`browser-automation.md`](browser-automation.md).
- Requires Python >= 3.11 (`requires-python` in `pyproject.toml`).

`pcli` with no subcommand launches the TUI; run it from a project directory
you want the agent to work in (relative paths in tool calls resolve against
`Path.cwd()` at startup).

## Running tests

```
pytest
```

`[tool.pytest.ini_options]` sets `testpaths = ["tests"]` and `asyncio_mode =
"auto"` (async `def test_...` functions run without needing an explicit
`@pytest.mark.asyncio` decorator).

## Linting / type-checking

```
ruff check
mypy src
```

`[tool.ruff]` sets `line-length = 100`, `target-version = "py311"`. `mypy` is
a dev dependency but has no dedicated `[tool.mypy]` config block in
`pyproject.toml` at present.

## Test layout

All tests live flat under `tests/` (no subpackages), one file per concern:

| File | Covers |
|---|---|
| `test_agent_loop.py` | `AgentLoop.run_turn`: tool dispatch and continuation, guardrail denial, ask-then-execute permission flow, invalid-arguments handling. |
| `test_artifact_tool.py` | `fetch_artifact`: missing store, unknown id, full-content and paged (offset/limit) retrieval, no-continuation-note on the last page. |
| `test_artifact_truncation.py` | `AgentLoop._archive_if_large`: truncation/archiving of large output, small output passing through untouched, no-store passthrough, custom threshold, a full round trip through `fetch_artifact`. |
| `test_artifacts.py` | `SessionArtifactStore`: put/get roundtrip, unknown id, distinct ids per put, blob-naming convention. |
| `test_builtin_tools.py` | `read_file`/`write_file` roundtrip and not-found, `list_dir`, `glob_search`, `grep` (matches + invalid regex). |
| `test_chat_screen_config_persist.py` | `/models` in the TUI persisting the selected model to `config.toml`. |
| `test_cli_config_persist.py` | CLI flags persisting gateway URL/model/artifact-threshold but never the API key; a bare `pcli` invocation not touching `config.toml`. |
| `test_config_settings.py` | `update_config_file`: new keys, merging with existing content, skipping falsy values, escaping special characters; `Settings` picking up persisted values. |
| `test_context_tracking.py` | `ContextLimitTable` exact/wildcard/default lookup and file overrides; `current_context_usage` (last-turn-only, empty session); `ContextUsage.fraction`; `format_token_count`. |
| `test_cost_tracker.py` | `PricingTable` lookup, `CostTracker` turn aggregation, `estimated` usage flagging, `global_cost_report` aggregating across sessions. |
| `test_llm_client.py` | `GatewayClient.chat_stream`: text/usage reconstruction, fragmented tool-call reassembly, non-retryable-error-raises-immediately, retry-then-success; `Settings.is_configured()`. |
| `test_permissions.py` | `GuardrailsConfig.evaluate_command`/`evaluate_path`; `PermissionPolicy` remember/check at session scope. |
| `test_pydiscovery.py` | Index build (stdlib coverage, no private names); `search_python` never imports anything; `call_python` (stdlib call, dict-arg JSON handling, unknown-module error); `inspect_python_module` member discovery. |
| `test_sandbox_docker.py` | `DockerSandbox` actually running a command (requires Docker available). |
| `test_sandbox_subprocess.py` | POSIX `preexec_fn` not double-calling `setsid`; argv-list and shell-string execution; cwd-outside-allowed-roots rejection; env scrubbing hiding secrets. |
| `test_session_roundtrip.py` | Export/import roundtrip (with and without `restore_grants`); rejecting a newer format version or wrong format; gzip export roundtrip; `SessionStore` list/delete. |
| `test_shell_passthrough.py` | `!`/`!!` passthrough: stdout capture, stderr + nonzero exit, cwd, **no env scrubbing** (unlike the sandbox), timeout kill, output truncation. |
| `test_status_pane.py` | `ActivityTracker` start/progress/finish; `StatusPane` hidden-when-empty, all todos shown as individual widgets in order, auto-scroll to the `in_progress` item (or home when none), hiding again once cleared. |
| `test_subagent_tool.py` | `spawn_subagent`: missing-context error, final text + usage, the registry never containing itself (no re-nesting), `allowed_tools` filtering, actually calling a tool, activity-progress reporting. |
| `test_todo_tool.py` | `write_todos`: no-session error, setting todos, replace-not-append semantics, rejecting multiple `in_progress`, rejecting non-list/malformed entries. |
| `test_toolbox_introspect.py` | Subcommand-guessing regex; `collect_help_corpus` walking guessed subcommands; `synthesize_tools` valid response, retry-once-then-succeed, give-up-after-two-invalid-attempts, schema-violation rejection. |
| `test_toolbox_manager.py` | Registry/synthesized-schema store roundtrips; corpus hashing; curated-plugin discovery; unknown-binary error; `load_all` rebuilding curated tools and skipping missing ones. |
| `test_toolbox_plugin_kubectl.py` | Pure-Python plugin logic with no real binaries needed: kubectl version parsing, tool shape/risk tiers, `get`/`delete` arg building; httpd version parsing; SGE skipping missing sibling binaries. |
| `test_tui_app.py` | Mounts the real `PcliApp` with its actual `CSS_PATH`, unlike every other TUI test here which mounts `ChatScreen` on a bare `App` subclass. This is what actually parses `pcli.tcss` at test time, so it's the guard against invalid/broken CSS rules reaching a real build — a failure class the bare-`App` tests elsewhere in the suite don't exercise. |

A recurring pattern worth reusing for new tests: sandbox/toolbox plugin tests
avoid needing the real binary wherever the logic under test (arg-building,
schema shape, version parsing) is pure Python over a fake `DetectionResult`/
`ExecResult` — only `test_sandbox_docker.py` needs Docker actually installed
and running.

## Adding a new tool

To add a new built-in tool: write a handler + `ToolSpec` in
`src/pcli/tools/builtin/` (see [`tools.md`](tools.md) for the `ToolSpec`
shape — `needs_permission`, `needs_sandbox`, and the three
`guardrail_*_arg` fields), register it in `build_default_registry()`
(`src/pcli/tools/registry.py`), and add a test file mirroring
`test_builtin_tools.py`'s or `test_todo_tool.py`'s pattern (construct a
minimal `ToolContext`, call the handler directly, assert on `ToolResult`).

To add a new curated toolbox plugin: implement a `ToolboxPlugin` subclass in
`src/pcli/tools/toolbox/plugins/`, add it to `ALL_PLUGINS`
(`plugins/__init__.py`), and test it the way
`test_toolbox_plugin_kubectl.py` does — pure-Python assertions against
`build_tools()`/`build_args` given a fake `DetectionResult`, no real binary
required.
