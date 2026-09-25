# pcli documentation

pcli is an opencode-style AI coding agent CLI/TUI for Python. It talks to any
OpenAI-compatible chat-completions gateway, runs a tool-calling agent loop
against your working directory, and renders everything in a Textual TUI.

## Architecture at a glance

`pcli.cli` (Typer) parses flags/env, builds a `Settings` object, and either
launches a subcommand (`sessions`, `cost`, `toolbox`, `run` — the last being
a one-shot non-interactive task runner, see
[`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md)) or starts
the TUI. The TUI's `ChatScreen` (`pcli.tui.screens.chat`) is the hub: it owns the `Session`,
a `SessionStore`, and on mount builds a `Sandbox` (via `select_sandbox`), a
`ToolRegistry` (builtin tools + any discovered toolbox plugins), a
`PermissionManager`, a `GatewayClient`, and an `AgentLoop` that ties them
together. When the user submits a message, `ChatScreen` hands the message
history to `AgentLoop.run_turn`, which streams from `GatewayClient`, dispatches
any tool calls the model makes (each gated through `PermissionManager`, then
executed against `ToolContext.sandbox`), truncates/archives oversized tool
results to the artifact library, and yields events back to `ChatScreen` to
render and fold into the persisted `Session`. Cost (`CostTracker`) and context
usage are tracked per LLM call and shown in the status bar.

```
TUI (ChatScreen) -> AgentLoop -> GatewayClient (LLM gateway)
                              -> ToolRegistry -> tool handlers -> Sandbox
                  -> PermissionManager (guardrails + ask/remember)
                  -> SessionStore (persistence) + CostTracker
```

## Where to look

- [`configuration.md`](configuration.md) — every setting, env var, CLI flag,
  config.toml key, and the precedence/persistence rules.
- [`tools.md`](tools.md) — every built-in tool the LLM can call, and the
  automatic artifact-archiving mechanism for large tool output.
- [`tui-guide.md`](tui-guide.md) — using the interactive TUI: chat input
  (including Shift+Insert OS-clipboard paste), slash commands, shell
  passthrough (including the `!!!` real-terminal handoff), the decision log,
  permission prompts, status bar/pane.
- [`sandbox-and-permissions.md`](sandbox-and-permissions.md) — the sandbox
  backends and the guardrail/permission system that gates tool execution.
- [`sessions-and-cost.md`](sessions-and-cost.md) — session persistence,
  export/import, and cost/context tracking.
- [`memory.md`](memory.md) — pcli's global, cross-session user memory: the
  data model, the `remember` tool, autonomous extraction piggybacked on
  auto-compaction, system-prompt injection, and the `/memory` command.
- [`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md) — the
  shared non-interactive runtime (`agent/runtime.py`, `agent/headless.py`)
  and `pcli run`, the one-shot "run a task and exit" command that an OS
  scheduler (cron / Task Scheduler) or pcli's own scheduler can invoke.
- [`scheduling.md`](scheduling.md) — `pcli schedule`, pcli's own
  crontab-like recurring task scheduler: the `ScheduleJob` model,
  `schedule.json` persistence, the polling daemon (`pcli schedule run`),
  and the `add`/`list`/`remove`/`enable`/`disable`/`run-now` subcommands.
- [`browser-automation.md`](browser-automation.md) — driving a real
  Chromium browser via Playwright: the seven `browser_*` tools, the
  shared/persistent `BrowserSession`, and the headed-vs-headless default
  split between the TUI and `pcli run`.
- [`telegram-bot.md`](telegram-bot.md) — `pcli telegram`, a long-running
  two-way Telegram bot front end: the single-authorized-chat security
  model, inline-button permission approval, the message-queue
  serialization design, and the `pcli run --notify-telegram` flag.
- [`toolbox-plugins.md`](toolbox-plugins.md) — discovering OS/software tools
  (kubectl, SGE, Kafka, httpd, ...) for the agent to use.
- [`agent-tools-guide.md`](agent-tools-guide.md) — how to create and register
  a named, reusable subagent persona (an "agent tool") via
  `register_agent_tool`, or add a new built-in default.
- [`development.md`](development.md) — dev environment setup, running tests,
  linting, and test-layout tour.

Source of truth throughout is `src/pcli/` — this documentation describes what
the code does, not aspirational behavior.
