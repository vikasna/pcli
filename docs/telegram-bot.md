# Telegram bot

`pcli telegram` (`src/pcli/cli.py`) runs pcli as a long-running, two-way
Telegram bot — the third way to run pcli, alongside the interactive TUI and
the one-shot `pcli run` ([`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md)).
It's built on that same non-interactive foundation: every incoming message
is driven through `run_headless_task` (`agent/headless.py`), the identical
turn-driving machinery `pcli run` uses, just with `ask` wired to a Telegram
implementation instead of `None` (see [Permission approval over inline
buttons](#permission-approval-over-inline-buttons) below).

Implementation lives in `src/pcli/telegram/` — four modules, discussed in
[Layered module design](#layered-module-design) below.

## Installing

python-telegram-bot is an optional dependency — the `telegram` extra
(`pyproject.toml`):

```
pip install -e ".[telegram]"
```

Unlike the `browser` extra ([`browser-automation.md`](browser-automation.md#installing)),
which needs a second step (`playwright install chromium`, a separate ~300MB
binary download), the `telegram` extra is self-sufficient — installing it
is the whole setup. There's no second download; a bot token from
[@BotFather](https://t.me/BotFather) is all that's left (see below).

## Required settings

Two `Settings` fields (`src/pcli/config/settings.py`), both **env-var/
config-kwarg only, never persisted to `config.toml`**:

- **`telegram_bot_token`** (env `PCLI_TELEGRAM_BOT_TOKEN`) — the bot's
  token from @BotFather. `repr=False` (never printed/logged) and
  `update_config_file` is never called with it, the same treatment as
  `gateway_api_key` — see [`configuration.md`](configuration.md#auto-persisted-settings).
  It's a secret; requiring it to be supplied fresh (env var, or a
  constructor kwarg) every time rather than sitting in a plaintext
  `config.toml` on disk is deliberate.
- **`telegram_chat_id`** (env `PCLI_TELEGRAM_CHAT_ID`) — the *one*
  authorized chat id, default `0` meaning unset. Not itself secret, but
  central to the security model below, so it isn't auto-persisted either.

`Settings.is_telegram_configured()` returns `True` only when both are set
(`bool(telegram_bot_token) and telegram_chat_id != 0`). Both `pcli
telegram` and `pcli run --notify-telegram` gate on this before doing
anything — see below.

## Security model: one authorized chat, everything else silently ignored

This is personal automation, not a multi-user bot, and the design commits
to that throughout:

- **`pcli telegram` refuses to start unconfigured.** Before doing anything
  else, `telegram_command` (`cli.py`) checks `settings.is_configured()`
  (the gateway) and `settings.is_telegram_configured()`, printing a clear
  message and exiting with code `1` if either is missing:

  ```
  Gateway not configured. Set PCLI_GATEWAY_URL (and PCLI_GATEWAY_API_KEY if your gateway requires auth) or edit the config file first.
  ```

  ```
  telegram_bot_token and telegram_chat_id must both be set (env vars PCLI_TELEGRAM_BOT_TOKEN / PCLI_TELEGRAM_CHAT_ID, or the constructor kwargs) before pcli telegram can start.
  ```

  This is a real security boundary, not a soft warning — there's no
  "running but half-configured" state.

- **Anything from another chat is silently ignored, not refused.**
  `TelegramDaemon._is_authorized` (`telegram/daemon.py`) compares the
  incoming `chat_id` against the single configured
  `settings.telegram_chat_id`; an unauthorized message is dropped with
  only a warning-level log line (`"Ignored message from unauthorized chat
  id %s"`) — the bot never replies to a stranger, so it doesn't even
  confirm to them that it exists and is listening. The `/new` command gets
  the same check.

- **Button presses need no separate authorization check.** A permission
  approval's `callback_data` (`perm:<8 hex chars>:<allow|deny>:<once|
  session|always|->`) is only meaningful if it matches a request id this
  exact process handed out via `PendingApprovals.register` — there's
  nothing for an unauthorized chat to forge even if it somehow saw a
  button, so `TelegramDaemon.handle_callback` deliberately does no chat-id
  check of its own (see the docstring on that method).

## `pcli telegram`

```
pcli telegram
```

Takes no flags. Once actually polling, it prints:

```
Listening for chat <chat_id>. Session: <session_id>
Press Ctrl+C to stop.
```

and runs until interrupted, printing `Stopped.` on Ctrl+C. If startup
itself fails after the configuration checks above pass (e.g. an invalid
token rejected by Telegram), it prints `Startup failed: <error>` and exits
`1`.

**One ongoing session per daemon run.** `TelegramDaemon` (`telegram/
daemon.py`) owns a single `Session`, built via `new_headless_session`
(`agent/headless.py`) — the same "fresh session, same system prompt a
brand-new TUI session gets" default `pcli run` uses with no `--session`.
Because it's a real `Session` persisted via `SessionStore`, it gets
`pcli sessions list`/`--resume`/export like any other session — the
`Session: <id>` line printed on startup is there precisely so you can
`pcli --resume <id>` into it later, e.g. to inspect what the bot did while
you were away. **Restarting `pcli telegram` starts a new session** — the
same "no `--session` given" default as a plain `pcli run`, not a resume of
the previous one.

### The `/new` command

Sent as a message from the authorized chat, `/new` resets the daemon's
session mid-run *without restarting the daemon* — `TelegramDaemon.
handle_new_command` calls `new_headless_session` again and replies "Started
a new session." This is the one way to get a fresh conversation without
killing and re-launching the whole process (and its polling connection).

### Settings commands

Ten more slash commands bring TUI-style setting view/set parity to
Telegram — each works the same way its `chat.py` counterpart does: with
no argument it replies with the setting's current value, with one
argument it parses, validates, and persists the new value (replying with
a confirmation, or an explanation if the value was invalid). The TUI
command name differs for several of these, since the TUI allows hyphens
and Telegram doesn't (see below):

| Telegram command | TUI equivalent |
| --- | --- |
| `/timeout` | `/timeout` |
| `/temperature` | `/temperature` |
| `/budget` | `/budget` |
| `/context_limit` | `/context-limit` |
| `/max_tool_iterations` | `/max-tool-iterations` |
| `/artifact_threshold` | `/artifact-threshold` |
| `/max_tool_calls_per_turn` | `/max-tool-calls-per-turn` |
| `/max_tool_calls_per_minute` | `/max-tool-calls-per-minute` |
| `/prune_tool_results` | `/prune-tool-results` |
| `/max_response_tokens` | `/max-response-tokens` |

**Why the names differ: Telegram bot commands can only contain letters,
digits, and underscores** — no hyphens — a hard platform limitation
(verified directly against Telegram's `bot_command` entity rules), not a
stylistic choice. Every hyphenated TUI command name above is spelled with
underscores instead on the Telegram side (e.g. `/context-limit` becomes
`/context_limit`).

All ten are registered through a single `CommandHandler` (`bot.py`)
covering all ten names at once, which dispatches by command name to the
matching `TelegramDaemon.handle_*_command` method. Each of those methods
is a thin wrapper around one shared helper,
`TelegramDaemon._handle_scalar_setting_command` (`daemon.py`), which holds
the common view-or-set/validate/persist logic so it isn't duplicated ten
times over.

One behavioral difference from the TUI: `chat.py`'s handlers call
`self._agent_loop.set_*(...)` to apply a change to the TUI's one
long-lived `AgentLoop` immediately. The Telegram versions skip that
step — `run_headless_task` already builds a fresh `AgentLoop` from
`Settings` on every incoming message, so persisting the setting is
already enough for it to take effect on the next message.

### `/rename`, `/allowed_roots`, `/memory`, and `/help`

Four more commands round out parity with `chat.py`, each mirroring its
`chat.py` counterpart's logic directly rather than going through the
shared scalar-setting helper above:

- **`/rename [name]`** — view or set the current session's title, same
  as `chat.py`'s `/rename` (no hyphen, so the name carries over
  unchanged). With no argument, replies with the current title
  (`Session.derive_title()`); with one, sets `Session.title` and
  persists it via `SessionStore`.
- **`/allowed_roots [add|remove] [path]`** — view or edit the filesystem
  guardrail's `fs_allowed_roots` list. Telegram name for the TUI's
  `/allowed-roots` (hyphen -> underscore, the same platform constraint
  as the table above); `add` and `remove` are plain arguments, not
  command names, so they keep their TUI spelling unchanged. Refuses to
  remove the last remaining root — the agent needs at least one, or
  every filesystem tool call would be denied. Edits apply immediately
  via `update_guardrails_fs_allowed_roots` (`permissions/guardrails.py`).
- **`/memory [forget <id>|clear]`** — view, trim, or clear pcli's
  cross-session memory of you (`memory/store.py`), same as `chat.py`'s
  `/memory`. With no argument, replies with `render_memory_list` of the
  current entries; `forget <id>` removes the one entry whose id ends
  with the given suffix (replying with an error if none or more than one
  match); `clear` wipes every entry.
- **`/help`** — a Telegram-specific command reference, and a different
  constant from `chat.py`'s own `_HELP_TEXT`, not just a resend of it.
  `chat.py`'s version is Markdown-formatted (`**bold**`), but
  `BotSender.send_message` (`telegram/sender.py`) sends with no
  `parse_mode` set, so Markdown there would show up as literal asterisks
  in the Telegram client. `daemon.py` instead defines its own plain-text
  `_HELP_TEXT` constant, listing only the commands actually implemented
  in Telegram so far.

All four get the same "silently ignored from an unauthorized chat" check
as every other command.

### Other slash commands

`/new`, the ten settings commands, and the four commands above are the
only slash commands implemented here — Telegram has no equivalent of the
TUI's full command set yet (`/models`, `/sessions`, `/plan`, ...).
Sending any other `/`-prefixed message is caught by a fallback
`MessageHandler(filters.COMMAND, ...)` (`bot.py`) →
`TelegramDaemon.handle_unsupported_command`, which replies that the
command isn't supported yet. Previously nothing matched `filters.
COMMAND` except the registered `/new` handler, so anything else was
silently dropped by python-telegram-bot itself before `TelegramDaemon`
ever saw it — indistinguishable from the message never arriving at all.

### Shell passthrough: `!command` and `!!command`

A message starting with `!` doesn't go to the LLM at all —
`TelegramDaemon.handle_text` routes it straight to `handle_shell_passthrough`,
which mirrors the TUI's own `!command`/`!!command` passthrough
([`tui-guide.md`](tui-guide.md#shell-passthrough-command-command-and-command)):
it runs the command directly via the same `run_passthrough_command`
(`tui/shell_passthrough.py`), bypassing the LLM, the sandbox, the
permission system, and session/artifact recording entirely. `!<command>`
replies with `$ command`, its stdout/stderr, and `[exit_code=N]`;
`!!<command>` runs the same way but replies with `(output hidden)`
instead of the result. The chat-id authorization check still applies —
an unauthorized chat gets nothing, same as any other message.

The TUI's third tier, `!!!command` (handing the real terminal off to the
command via `App.suspend()`), has no Telegram equivalent — there's no TTY
on the other end of a chat to hand off to — so sending `!!!` here gets an
explanatory reply instead of being attempted.

### Live tool-call progress

A turn with tool calls used to be silent until it finished — just the
upfront "Working on it..." and then the final reply, with nothing in
between no matter how many tool calls it made. `TelegramDaemon._process`
now wires up `run_headless_task`'s `on_progress` callback (`agent/
headless.py`) so each tool call and its result reach the chat as separate
messages while the turn is still running: `  -> tool_name(args)` when the
call is made, then `  <- result...` with a truncated preview once it
returns.

`on_progress` is a plain synchronous callback — it's invoked from inside
an `async for` loop in `run_headless_task` that `_process` doesn't
control, so it can't `await` the real Telegram send itself. It just puts
the line onto a small `asyncio.Queue`; a background task drains that
queue and does the actual sending, so progress forwarding never blocks or
reorders the turn-driving loop. That queue is always drained — sending a
sentinel and awaiting the drain task — before the final reply or a
`GatewayError` message goes out, so tool-call progress reliably appears
ahead of whatever concludes the turn, never after.

One line is deliberately filtered out: the initial `"> {task}"` progress
line that just echoes the user's own message back is skipped, since
Telegram already shows what they sent.

## Permission approval over inline buttons

`ask_via_telegram` (`src/pcli/telegram/permissions.py`) is
`PermissionManager`'s `ask` callback (`permissions/manager.py`'s
`AskCallback`) implemented over Telegram — same signature, same
`PermissionModalResult` return shape as the TUI's `ask_via_modal`
(`tui/screens/permission_modal.py`). From the agent loop's perspective
it's a drop-in swap: nothing in `PermissionManager.check_with_reason`
(see [`sandbox-and-permissions.md`](sandbox-and-permissions.md#permission-manager))
needs to know or care whether `ask` pops a Textual modal or sends a
Telegram message.

When a tool call needs approval and no remembered grant already covers it,
`ask_via_telegram`:

1. Registers a pending approval (`PendingApprovals.register`) — an
   in-memory correlation between this specific prompt and whichever button
   press eventually answers it. Telegram's updates are async and decoupled
   (no request/response pairing built in), so each prompt gets a short
   random request id (`secrets.token_hex(4)`, 8 hex chars) embedded in
   every button's `callback_data`.
2. Sends a message describing the tool name, its arguments (truncated at
   800 chars — the same truncation precedent as the TUI modal's own
   `_MAX_ARG_PREVIEW_CHARS`, so a call with large arguments, e.g.
   `write_file`'s full text, can't blow past Telegram's 4096-char message
   limit), and the risk description — with **four inline buttons, the same
   options as the TUI's permission modal**: Allow Once, Allow for Session,
   Allow Always, Deny.
3. `await`s a future that resolves when the matching button press comes
   back through `TelegramDaemon.handle_callback` ->
   `PendingApprovals.resolve`.

**Nothing here depends on python-telegram-bot being installed.**
`permissions.py` only talks to a small `TelegramSender` protocol (send a
message, optionally with buttons) — the real PTB-backed implementation is
`BotSender` (`telegram/sender.py`), kept in a separate module precisely so
`ask_via_telegram`/`PendingApprovals` are fully unit-testable without the
package installed at all (see `test_telegram_permissions.py`).

**A pending approval is never persisted.** If the daemon restarts while a
prompt is outstanding, that future is simply lost — the same as a TUI
permission modal still open when the app is killed; there's no session
state to resume an in-flight approval into either way.

## Message-queue serialization

Before working a message, `TelegramDaemon._process` immediately replies
"Working on it..." — an upfront acknowledgment so a message sent mid-turn
doesn't look identical to one that was never received, since a turn can
take a while (several tool calls) with nothing else sent back until it
finishes. The real reply follows once the turn actually completes; what
happens in between is no longer silent either — see [Live tool-call
progress](#live-tool-call-progress) above.

`TelegramDaemon` processes incoming text messages **one at a time**,
through an `asyncio.Queue` (`handle_text` only ever `put`s onto the queue;
`run_forever` is the single consumer that pulls and processes). This
mirrors `ChatScreen`'s own "don't run two turns at once" discipline
(`_turn_in_progress` / queued followups) — just expressed as an explicit
queue instead of a Textual worker loop.

This matters because a turn mutates the shared `Session.messages` list as
it runs. If a second Telegram message arrived mid-turn and were handled
concurrently instead of queued, both turns would read and mutate that same
list at once — a race that could corrupt the session's message history.
Queueing means a message that arrives while the bot is still working
simply waits its turn rather than racing the one already in flight.

The one exception is `!`-prefixed [shell passthrough](#shell-passthrough-command-and-command)
— `handle_text` routes it straight to `handle_shell_passthrough` instead
of `put`ting it on the queue, so it runs immediately even mid-turn. This
is safe precisely because it never touches `Session.messages` (or
anything else the queue is protecting) at all.

## Layered module design

`src/pcli/telegram/` splits into four modules along the same "isolate the
raw third-party calls behind a thin, swappable seam" line that
[`browser/session.py` draws around Playwright](browser-automation.md#browsersession-lazy-launch-shared-session-persistent-profile):

- **`permissions.py`** — `PendingApprovals` and `ask_via_telegram` (see
  above). Zero dependency on python-telegram-bot; talks only to the
  `TelegramSender` protocol.
- **`sender.py`** — `BotSender`, the one place that actually imports the
  `telegram` package, and does so **lazily, inside each method** (not at
  module level) — the same discipline `browser/session.py` uses for its
  own Playwright import, and for the same reason: constructing a
  `BotSender` or importing this module at all should never require the
  optional dependency, only actually sending something should. This is
  what lets pcli import and run fine with the `telegram` extra not
  installed at all. `BotSender` also truncates any outgoing message over
  Telegram's 4096-char limit.
- **`daemon.py`** — `TelegramDaemon`, the business logic: owns the
  session, the message queue, authorization, and error handling described
  throughout this page. This is the heavily unit-tested layer
  (`test_telegram_daemon.py`, `test_telegram_permissions.py`) — it has no
  PTB dependency either, since it only calls through the `TelegramSender`
  protocol and plain asyncio primitives.
- **`bot.py`** — `run_telegram_daemon`, the actual python-telegram-bot
  `Application` wiring: builds the `Application`, registers a
  `CommandHandler("new", ...)`, a `MessageHandler` for plain text, and a
  `CallbackQueryHandler` for button presses, each just forwarding to the
  matching `TelegramDaemon` method, then runs until stopped. Deliberately
  thin — the business logic it wires up is what's heavily tested (above);
  this module gets lighter, fake-PTB coverage (`test_telegram_bot.py`)
  rather than an exhaustive one, since faithfully reproducing PTB's full
  `Application`/`Updater` lifecycle in a fake would cost far more than it
  would ever catch. Also defines `notify_telegram` — see
  [`pcli run --notify-telegram`](#pcli-run---notify-telegram) below.

## What happens on an unexpected error mid-turn

`TelegramDaemon.run_forever` wraps each queued message's processing in a
`try`/`except Exception`. A single bad turn — a bug in a tool, an
unexpected exception anywhere in `run_headless_task` — is caught, logged
(`logger.exception`), and reported back to the chat as:

```
Something went wrong handling that - check pcli's logs.
```

rather than killing the daemon. A long-running process shouldn't die
because one message went badly; the next queued (or future) message is
still processed normally. A `GatewayError` specifically is handled inside
`_process` and reported with its own message text
(`f"Gateway error: {exc.message}"`) instead of the generic one above, since
that's typically actionable (bad model name, gateway unreachable, ...)
rather than a pcli bug.

If a turn's response gets cut off by the token limit even after
`run_headless_task`'s own auto-continue retries are exhausted
(`truncations_exhausted` — see [Auto-continue on truncated
responses](headless-and-scheduled-runs.md#whats-different-from-the-tui)),
the daemon still sends whatever final text it has, followed by a second
message noting it may be incomplete and inviting another message to
continue.

## `pcli run --notify-telegram`

```
pcli run --task "..." --notify-telegram
```

A lightweight alternative to the full daemon above: after a scheduled
`pcli run` finishes, also sends the final answer text to the configured
Telegram chat as a single one-off message, via `notify_telegram`
(`telegram/bot.py`) — a standalone `telegram.Bot` and one `send_message`
call, no `Application`, no polling loop, no `TelegramDaemon`. This is
meant for exactly the case `pcli run` itself targets
([`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md)): a
cron/Task Scheduler job that runs unattended and should ping you on your
phone when it's done, without needing a whole bot daemon running just to
send that one message.

Both failure modes are non-fatal to the run itself:

- **Telegram isn't configured** (`settings.is_telegram_configured()` is
  `False`) — prints a warning to stderr and skips the notification; the
  run's own exit code is unaffected.
- **Sending fails** (network error, bad token, etc.) — also just a
  warning (`Failed to send the Telegram notification: <error>`), never a
  hard failure of the run.

This flag is independent of `--headed`
([`browser-automation.md`](browser-automation.md#headed-vs-headless)) —
either, both, or neither can be passed to a given `pcli run` invocation.
