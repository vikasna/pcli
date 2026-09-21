# Headless and scheduled runs

pcli's agent loop (`AgentLoop.run_turn`, `src/pcli/agent/loop.py`) doesn't
inherently need a TUI — it just needs something to stream events into and
something to answer tool-permission/question callbacks. `src/pcli/agent/
runtime.py` and `src/pcli/agent/headless.py` factor the UI-agnostic setup and
turn-driving logic out of `ChatScreen` so a non-interactive front end can
drive the same machinery without pulling in Textual at all.

Today there are two such front ends: `pcli run` (below), a one-shot "run
this task and exit" command meant to be invoked by an OS scheduler, and
`pcli telegram`, a long-running daemon that answers incoming messages over
Telegram — see [`telegram-bot.md`](telegram-bot.md) for that one.
[Browser automation](browser-automation.md) has also landed on this
foundation: `pcli run` shares the same `AgentRuntime`/`BrowserSession`
wiring the TUI uses, just defaulting to headless (see `--headed` below).

## Shared runtime building blocks

- **`build_agent_runtime(settings, cwd) -> AgentRuntime`** (`agent/
  runtime.py`) — the same sandbox selection, tool registry construction
  (builtins + toolbox plugins + persisted agent tools, with the same
  local-api-only `ask_artifact` filter `ChatScreen.on_mount` applies), and
  `GatewayClient` setup that the TUI does at startup. It deliberately skips
  two TUI-specific things: context-limit auto-detection (tied to
  `ChatScreen`'s own retry state — a headless run just uses whatever
  `ContextLimitTable` already has) and any progress notices (there's no
  message view to print them into).
- **`build_permission_manager(settings)`** (`agent/runtime.py`) — loads
  `guardrails.toml` and applies the same local-api rate-limit override
  `ChatScreen.__init__` used to inline. See [Local-API
  mode](configuration.md#local-api-mode).
- **`effective_max_tool_iterations(settings)`** (`agent/runtime.py`) — `None`
  (unlimited) in local-api mode, `settings.max_tool_iterations` otherwise.
- **`make_tool_context(...)`** (`agent/runtime.py`) — builds a `ToolContext`
  generically for any front end; `ask`/`ask_question` default to `None`
  (no UI to ask through — see [No permission prompts](#no-permission-prompts-pre-grant-always-allow-first)
  below).
- **`run_headless_task(...)`** (`agent/headless.py`) — drives a task through
  `AgentLoop.run_turn` end to end, reporting progress via a plain callback
  instead of `MessageView`. See [What's different from the
  TUI](#whats-different-from-the-tui) below for exactly what it does and
  doesn't replicate from `ChatScreen._run_one_turn`.
- **`new_headless_session(store, settings, cwd) -> Session`** (`agent/
  headless.py`) — builds a fresh `Session` with the same system prompt a
  brand-new TUI session gets, including the [global user
  memory](memory.md) section if `memory_enabled`.

## `pcli run`: one-shot task execution

```
pcli run --task "..."
pcli run --task-file path/to/task.txt
pcli run --task "..." --session <id>
pcli run --task "..." --quiet
pcli run --task "..." --headed
pcli run --task "..." --notify-telegram
```

| Flag | Behavior |
|---|---|
| `--task TEXT` | The task, given inline. Exactly one of `--task`/`--task-file` is required. |
| `--task-file PATH` | Path to a file containing the task. Meant for longer, reusable, step-by-step instructions — e.g. a task you've worked out once and now invoke repeatedly on a schedule. |
| `--session ID` | Append to an existing session (`pcli sessions list` for ids) instead of starting a fresh one via `new_headless_session` — gives a scheduled job continuity across runs, the same session history and cost totals carrying forward each time it fires. |
| `--quiet` | Print only the final answer text, not tool-call progress lines — for a clean cron-job log. |
| `--headed` | Show the browser window if any `browser_*` tool gets used, instead of the headless default. See [`browser-automation.md`](browser-automation.md#headed-vs-headless) for why `pcli run` defaults to headless while the TUI defaults to headed. |
| `--notify-telegram` | After the run finishes, also send the final answer text to the configured Telegram chat — a lightweight one-off message (`notify_telegram`, `telegram/bot.py`), not the full `pcli telegram` daemon/polling machinery. If `telegram_bot_token`/`telegram_chat_id` aren't configured, this prints a warning and skips rather than failing the run; if sending itself fails (network, bad token, ...), that's also just a warning. See [`telegram-bot.md`](telegram-bot.md#pcli-run---notify-telegram). |

Passing both or neither of `--task`/`--task-file` is a usage error (exit
code 1, "Provide exactly one of --task or --task-file."). All the normal
`pcli` gateway flags/env vars (`--gateway-url`, `--model`, `PCLI_GATEWAY_URL`,
...) still apply the same way — see the [top-level
README](../README.md#configuring-the-gateway); `pcli run` reads `Settings`
the same way the TUI does and exits with an error if the gateway isn't
configured at all.

Without `--quiet`, `run_headless_task`'s `on_progress` callback prints each
step as it happens: the task itself (`> ...`), each tool call
(`  -> tool_name(arguments)`), and each tool result, truncated to 200
characters (`  <- ...`). With `--quiet`, only the final assistant text
prints.

On completion, `pcli run` always prints the final answer and a resume hint:

```
Session: <id> (resume with: pcli --resume <id>)
```

### No permission prompts — pre-grant "Always Allow" first

There's no UI for `pcli run` to prompt through, so it constructs its
`ToolContext` with `ask=None` and `ask_question=None`. This isn't a gap that
needed new code — both existing mechanisms already handle an absent UI
safely, and `pcli run` is simply the first caller to exercise that path for
real:

- **Permission checks** (`PermissionManager.check_with_reason`, see
  [`sandbox-and-permissions.md`](sandbox-and-permissions.md#permission-manager))
  already check a persisted **"Always Allow"** grant *before* ever needing
  `ask`. Only if no remembered grant exists does it fall back to calling
  `ask` — and with `ask=None`, that step **fails closed**: the tool call is
  denied with reason `"no UI available to request approval"`.
- **`ask_user_question`** (`tools/builtin/ask_tool.py`) already degrades
  gracefully when `ask_question` is `None`: it returns an error result
  telling the model to "state your assumption plainly instead and proceed
  with it, rather than blocking on an answer you can't get here" — the model
  keeps working instead of hanging.

The practical consequence: **anything a scheduled task needs to actually do
— run a particular shell command, write outside the default allowed roots,
whatever the guardrails would otherwise stop and ask about — must already
have "Always Allow" granted interactively first**, via the TUI's permission
prompt (`PermissionPromptModal`). "Always" grants persist to
`permissions.json` (`config_dir()/permissions.json`) and apply across
processes, so a grant made once interactively is picked up by every later
`pcli run` invocation automatically. A task that hits a tool with no
standing grant will simply get it denied (visible in the progress output,
or in the final answer if the model reports the failure) rather than pausing
to ask — there's nothing to unblock it once a scheduled run is already
underway unprompted.

### What's different from the TUI

`run_headless_task` deliberately does not replicate everything
`ChatScreen._run_one_turn` does. Left out, on purpose:

- **Tool-result pruning** ([`configuration.md`](configuration.md#tool-result-pruning))
- **Auto-compaction** ([`configuration.md`](configuration.md#auto-compaction))
- **Memory extraction** ([`memory.md`](memory.md#autonomous-extraction-derived))

All three are tied to `ChatScreen`'s own `StatusBar`/`MessageView` plumbing
and are aimed at a long-lived interactive session accumulating history over
hours. A headless run is normally short-lived per invocation, so none of
that machinery is wired in here.

One piece *is* replicated: **auto-continuing a response truncated by the
token limit** — the same `TurnCompleteEvent.response_truncated` mechanism
documented for the TUI in [Auto-continue on truncated
responses](tui-guide.md#auto-continue-on-truncated-responses). Skipping it
here would be a worse bug than in the TUI: there's no user present to notice
a response that trails off mid-sentence and type "continue" themselves.
When a turn's final response is cut off by the token cap, `run_headless_task`
appends the same synthetic `"Continue."` user message and runs another turn
automatically, printing a progress line each time
(`[pcli] Response was cut off by the token limit - continuing automatically
(N/3)`). This is capped at **3 consecutive auto-continues**
(`_MAX_CONSECUTIVE_AUTO_CONTINUES`), the same cap and reasoning as the TUI's
own — if the cap is hit, `HeadlessTurnResult.truncations_exhausted` is set
instead of continuing further (see [Exit codes](#exit-codes) below).

### Exit codes

`pcli run` exits:

- **`0`** — the turn finished normally.
- **non-zero (`1`)** — if the turn hit its tool-iteration cap
  (`HeadlessTurnResult.terminated_early`, i.e. `max_tool_iterations` — see
  [`configuration.md`](configuration.md#local-api-mode)) or exhausted the
  truncation-retry budget above (`truncations_exhausted`). Also used for
  ordinary usage errors: missing/malformed flags, an unconfigured gateway, an
  unknown `--session` id, or a sandbox startup failure.

This makes a scheduled `pcli run` invocation scriptable/monitorable from
cron or Task Scheduler: a non-zero exit means the task likely didn't finish
its work, distinct from a clean `0` finish.

### Scheduling is the OS's job, not pcli's

`pcli run` is only the "run one task and exit cleanly" primitive — pcli
itself has no scheduler or daemon loop. Recurrence is left to whatever
scheduler the OS already provides:

```cron
# crontab: every day at 07:00, appending to the same running session
0 7 * * * cd /path/to/project && pcli run --task-file /path/to/daily-report.txt --session sess_abc123 --quiet >> /var/log/pcli-daily.log 2>&1
```

On Windows, the equivalent is a Task Scheduler task whose action runs `pcli
run --task-file ... --quiet` with the working directory set to the target
project, on whatever trigger (daily, at logon, ...) fits.
