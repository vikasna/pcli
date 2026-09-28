# Scheduling

`pcli schedule` is pcli's own crontab-like recurring task scheduler — a
daemon you start once that then decides *when* each job fires, rather than
an OS scheduler (cron/Task Scheduler) re-invoking `pcli run` on its own
timer. It's built entirely on the existing non-interactive foundation: a
scheduled job firing runs through the exact same `run_task_once`
(`src/pcli/scheduler/runner.py`) that `pcli run` itself now calls — see
[`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md) for that
shared runtime, and [OS-level scheduling still works
too](headless-and-scheduled-runs.md#os-level-scheduling-still-works-too)
for when you'd rather keep using cron/Task Scheduler directly against a
plain `pcli run`.

Implementation lives in `src/pcli/scheduler/` (`models.py`, `store.py`,
`runner.py`, `daemon.py`) plus the `schedule_app` command group in
`src/pcli/cli.py`.

## Installing

`croniter` (standard 5-field cron expression parsing) is an optional
dependency — the `schedule` extra (`pyproject.toml`):

```
pip install -e ".[schedule]"
```

Self-sufficient, same shape as the `telegram` extra
([`telegram-bot.md`](telegram-bot.md#installing)) — no separate binary or
download step, unlike the `browser` extra's Chromium download. `croniter`
is imported lazily, inside `scheduler/daemon.py`'s `compute_next_run` and
inside `cli.py`'s `schedule_add` command — never at module top level — so a
plain `pcli` (and even the rest of `pcli schedule`'s own subcommands, like
`list`/`remove`/`enable`/`disable`) still imports and runs fine without
this extra installed. Only actually validating/computing a cron schedule
needs it: `pcli schedule add` and the daemon's next-run computation.

## `ScheduleJob`: what a job stores

Persisted as `ScheduleJob` (`scheduler/models.py`, a pydantic model), one
per recurring job:

| Field | Meaning |
|---|---|
| `id` | Auto-generated (`new_id("job_")`), e.g. `job_a1b2c3d4`. |
| `name` | Optional friendly label shown in `pcli schedule list`; defaults to `""` (shown as `(unnamed)`). |
| `cron` | Standard 5-field cron expression (minute hour day month weekday), e.g. `"*/15 * * * *"`. Validated with `croniter.is_valid` before the job is ever saved. |
| `task` / `task_file` | Exactly one is set — mirrors `pcli run`'s own `--task`/`--task-file` mutual exclusivity. `task_file` is read fresh (`Path(...).read_text()`) each time the job fires, so editing the file changes what the next run does without needing to re-add the job. |
| `session_id` | Append to this one continuing session on every run, or `None` for a fresh session each time — mirrors `pcli run --session`. |
| `headed` | Show the browser window if a `browser_*` tool gets used, instead of the headless default. |
| `notify_telegram` | Send the final answer to the configured Telegram chat after each run — same mechanism as [`pcli run --notify-telegram`](telegram-bot.md#pcli-run---notify-telegram). |
| `quiet` | Only keep the final answer in the run's progress output, not tool-call-by-tool-call lines. Defaults to `True` (unlike `pcli run`, which defaults to verbose) — a scheduled job's output normally just goes to a log. |
| `max_cost_usd` | Per-job hard spend cap (USD), overriding `max_session_cost_usd` for just this job's own runs — mirrors `pcli run --max-cost`, applied the same way (`scheduler/daemon.py`'s `_run_one_job`, via `settings.model_copy`): never persisted to `config.toml`, never affects other jobs or the TUI. `None` (the default) means this job falls back to whatever `max_session_cost_usd` is already configured (unset by default — no cap). See [`configuration.md`](configuration.md#settings-fields) and [`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md#pcli-run-one-shot-task-execution). |
| `enabled` | Disabled jobs are skipped by the daemon entirely, but stay listed (and keep their `last_run_at`/`last_status` history) so re-enabling doesn't lose anything. |
| `created_at` | Set once, at creation. |
| `next_run_at` | The next time this job is due. Left unset (`None`) at creation on purpose — computed lazily by the daemon the first time it sees the job (`croniter(cron, base=now).get_next(datetime)`), so a job added while the daemon isn't running doesn't carry a stale "next run" computed from whenever `schedule add` happened to execute rather than from whenever the daemon actually starts watching it. |
| `last_run_at` / `last_status` / `last_error` | Bookkeeping updated after every run: `last_status` is `"ok"` or `"error"` (`None` if the job has never run); `last_error` holds the exception text (or `"session '<id>' no longer exists"`) when `last_status` is `"error"`. |

`ScheduleStore` (also `models.py`) is just `{jobs: list[ScheduleJob]}` — the
top-level shape written to disk.

## Persistence: `schedule.json`, read fresh every call

`scheduler/store.py` persists to a single JSON file —
`schedule_file()` (`config/paths.py`) → `data_dir() / "schedule.json"` —
atomically written (write to a `.tmp` file, then `os.replace`), mirroring
[`memory/store.py`](memory.md)'s exact shape. Deliberately **no in-memory
caching**: every read (`read_schedule`) goes straight to disk. This is what
lets `pcli schedule add`/`remove`/`enable`/`disable`, run from one process,
and the long-running daemon (`pcli schedule run`) in another process
always see each other's latest state — the daemon picks up an edit on its
very next poll tick without needing to be restarted.

The store's functions: `read_schedule`, `write_schedule`, `add_job`,
`remove_job`, `get_job`, `set_job_enabled`, `update_job` (replaces the
stored job with the same id — how the daemon persists
`next_run_at`/`last_run_at`/`last_status`/`last_error` after a run without
hand-rolling the read/mutate/write cycle itself).

## `pcli schedule` subcommands

### `add`

```
pcli schedule add --cron "*/15 * * * *" (--task "..." | --task-file path) [options]
```

| Flag | Behavior |
|---|---|
| `--cron TEXT` | Required. Standard 5-field cron expression. Rejected with a clean error (`'<cron>' isn't a valid 5-field cron expression.`, exit code 1 — not a crash) if `croniter.is_valid` says it's malformed. |
| `--task TEXT` / `--task-file PATH` | Exactly one required — same rule and error text (`Provide exactly one of --task or --task-file.`) as `pcli run`. |
| `--name TEXT` | Friendly label for `pcli schedule list`. |
| `--session ID` | Append to this existing session every run. |
| `--headed` | Show the browser window if used. |
| `--notify-telegram` | Notify Telegram after each run. |
| `--quiet` / `--no-quiet` | Defaults to `--quiet` (unlike `pcli run`, which defaults to verbose progress output). |
| `--max-cost AMOUNT` | Hard cap on this job's own runs (USD), overriding `max_session_cost_usd` just for it — never persisted to `config.toml`, never affects other jobs or the TUI. Omit to use whatever `max_session_cost_usd` is already configured (unset by default — no cap). Stored as the job's `max_cost_usd` field (table above). |

Prints the new job's id on success (`Added job job_xxxx (<cron>). Start
'pcli schedule run' to begin executing it.`). **Adding a job only saves
it** — nothing runs until the daemon (`pcli schedule run`) is actually
started.

### `list`

```
pcli schedule list
```

One line per job: id, name (or `(unnamed)`), `[cron]`, `enabled`/`disabled`,
`next: <next_run_at or "not yet computed">`, `last: <status @ timestamp, or
"never run">`. With no jobs, prints a hint to add one instead of an empty
table.

### `remove` / `enable` / `disable`

```
pcli schedule remove <id>
pcli schedule enable <id>
pcli schedule disable <id>
```

Each takes a job id (from `pcli schedule list`) and errors cleanly
(`No job found with id '<id>'.`, exit code 1) if it doesn't exist.
`disable` stops the daemon from firing the job on its next poll but keeps
it (and its run history) in the store; `enable` reverses that.

### `run` — the daemon

```
pcli schedule run [--poll-interval N]
```

The long-running foreground process (`run_scheduler_daemon`,
`scheduler/daemon.py`) — runs until interrupted with Ctrl+C. This is what
you point an OS-level scheduler, `systemd`, `nohup`, or a `tmux`/`screen`
session at to keep alive: **it is itself what decides *when* a job fires**,
not something an external cron needs to re-invoke per job or per tick.
`--poll-interval` (default `30`) sets the seconds between checks.

Checks the gateway is configured before starting (same message as `pcli
telegram`'s own check — see
[`telegram-bot.md`](telegram-bot.md#security-model-one-authorized-chat-everything-else-silently-ignored)):

```
Gateway not configured. Set PCLI_GATEWAY_URL (and PCLI_GATEWAY_API_KEY if your gateway requires auth) or edit the config file first.
```

Once running, it prints `Scheduler running (polling every <N>s). Press
Ctrl+C to stop.`, then one line per job it fires (`[job_id] <name or cron>
finished.` or `... failed: <error>` to stderr), and `Stopped.` on Ctrl+C.

**Each tick** (`_run_due_jobs` in `daemon.py`):

1. Re-reads `schedule.json` fresh from disk.
2. For every **enabled** job:
   - If `next_run_at` is unset, computes it (`croniter(cron,
     base=now).get_next(datetime)`), persists it, and moves on — it
     doesn't fire on the same tick it was first computed.
   - If `next_run_at` is still in the future, skips it.
   - Otherwise the job is due: runs it via `run_task_once`, records
     `last_run_at`/`last_status`/`last_error`, and advances `next_run_at`
     from the completion time (`compute_next_run(cron, base=last_run_at)`).
3. Sleeps `poll_interval_s`, then repeats.

**Jobs within one tick run sequentially, not concurrently** — simple,
predictable, and avoids two possibly-related scheduled tasks racing each
other over the same sandbox/session.

**A single job failing never crashes the daemon loop.** Both a generic
exception and a missing session (`ScheduledSessionNotFoundError`, raised by
`run_task_once` when `session_id` no longer resolves via `SessionStore`,
e.g. it was deleted via `pcli sessions`) are caught, logged
(`logger.exception`/`logger.error`), and recorded onto the job as
`last_status = "error"` / `last_error` — the daemon moves on to the next
job and the next tick exactly as if nothing had happened.

### `run-now`

```
pcli schedule run-now <id>
```

Fires one job **immediately**, bypassing its cron schedule entirely — for
testing that a job actually works (gateway reachable, task text correct,
session id valid, permissions pre-granted) before trusting the daemon's
own timing to fire it later. Prints the final answer text, same as `pcli
run`. Does **not** update `next_run_at`/`last_run_at`/etc. the way a
daemon-fired run does — it's a one-off, out-of-band execution, not a tick
of the schedule.

## Worked example

```
$ pcli schedule add --cron "0 7 * * *" --task-file daily-report.txt --name "daily report" --quiet
Added job job_9f3e1a2b (0 7 * * *). Start 'pcli schedule run' to begin executing it.

$ pcli schedule list
job_9f3e1a2b  daily report  [0 7 * * *]  enabled  next: not yet computed  last: never run

$ pcli schedule run-now job_9f3e1a2b
<final answer text from the run>

$ pcli schedule list
job_9f3e1a2b  daily report  [0 7 * * *]  enabled  next: not yet computed  last: ok @ 2026-09-26T09:03:11+00:00

$ pcli schedule run
Scheduler running (polling every 30s). Press Ctrl+C to stop.
[job_9f3e1a2b] daily report finished.
^C
Stopped.
```

(`run-now` doesn't touch `next_run_at`, which is why `list` still shows
"not yet computed" after it — that only gets filled in once the daemon
itself has ticked over the job at least once.)

Leave `pcli schedule run` running under `tmux`/`nohup`/a Windows
scheduled-at-logon task, and every enabled job fires on its own cron
schedule from then on, with no external cron re-invoking anything per job.
