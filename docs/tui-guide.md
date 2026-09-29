# TUI guide

Running `pcli` with no subcommand launches the Textual TUI (`PcliApp` ->
`ChatScreen`, `src/pcli/tui/`).

## Layout

Top to bottom (`ChatScreen.compose`):

- **Status pane** (`StatusPane`) — todo list only, ~4 lines, scrollable.
  Collapses to zero height when there's nothing to show.
- **Message view** (`MessageView`) — scrollable conversation history. Auto-
  scrolls to the bottom as new content streams in, but only while you're
  already there — scroll up at any point (e.g. to read an expanded tool
  result or "Thinking" panel) and further streamed content stops dragging you
  back down; scroll back to the bottom yourself and auto-scroll picks up
  again automatically.
- **Status bar** (`StatusBar`) — cost/context/tokens/sandbox/spinner, plus a
  second line with subagent progress while one is running.
- **Input box** — where you type.

## Quitting

**Ctrl+Q** quits. This isn't something pcli declares itself — it's Textual's
own `App.BINDINGS` default (`ctrl+q` -> `quit`, `priority=True`, so nothing at
the `Screen` level could ever override it). **Ctrl+C does not quit** —
Textual's `App` also binds `ctrl+c` itself, to `action_help_quit`, which just
shows a "Press ctrl+q to quit the app" notification rather than quitting,
deliberately, so a reflexive Ctrl+C doesn't kill the app. `ChatScreen.BINDINGS`
is `[]`; it used to declare its own `("ctrl+c", "quit", "Quit")` entry, but
that never actually fired (a same-key system-level binding on `App` always
wins over a same-key `Screen`-level one) and was removed.

Whenever pcli exits — Ctrl+Q or otherwise — `pcli.cli._root` prints a hint
afterward naming whichever session was most recently active during that run
(not necessarily the one it started with, since `/sessions` can switch to a
different one mid-run):

```
Resume this session anytime with: pcli --resume <id>
```

Skipped if that session never had a single message sent in it — those are
silently pruned on the next launch anyway (see
[`sessions-and-cost.md`](sessions-and-cost.md#pruning-empty-sessions)), so a
resume hint for one would point at a session that's about to vanish.

## Chat input

Type a message and press Enter to send it to the agent. It's appended to the
session, rendered in the message view, and streamed through `AgentLoop`,
which handles any tool calls the model makes (each may trigger a permission
prompt — see below) before the turn completes and the session is saved to
disk.

Anything starting with `/` is a slash command; anything starting with `!` is
shell passthrough, in one of three tiers (`!`, `!!`, `!!!` — all below). Plain
text otherwise goes to the model.

### Submitting while a turn is in progress

Submitting a message while pcli is still working on a previous turn no longer
cancels that turn. Instead of being appended to the transcript right away, it's
held in `ChatScreen._queued_followups` and shown immediately via
`MessageView.add_queued_user_message` — a dimmed `.message-queued` style that
visually distinguishes it as "received, will send once ready" — without being
written to `session.messages` yet. Only once the in-flight turn actually
finishes does `_stream_response` fold each queued message into
`session.messages`, in order, and start a follow-up turn for it. If you keep
typing and submitting while pcli is still busy, each message queues up and
runs in turn, back-to-back, with nothing lost or interrupted along the way —
the same queued-input behavior you may know from opencode or Claude Code.

This deliberate delay fixes a real bug: appending the message and showing it
immediately, on the same event-loop turn the keystroke arrived, could land it
*before* the in-flight turn's own assistant reply in `session.messages`
(that reply isn't appended until `turn_complete` fires), corrupting the
order of conversation history sent to the LLM on the next turn — and, if
typed while the assistant's reply was actively streaming, calling the normal
`MessageView.add_message` would silently hijack the widget streaming text
was being appended into, redirecting the rest of that reply into the new
message's own widget instead.

The input box is a `ChatInput` (`src/pcli/tui/widgets/chat_input.py`), a
`TextArea` subclass — Textual's `Input` is fundamentally single-line (there's
no incremental path to multi-line from it), so an auto-growing, multi-line-
capable box needed a different base widget. `ChatInput` layers several things
on top of plain `TextArea`: Enter-to-submit with an explicit newline key,
auto-grow, and paste collapsing (all below), plus Up/Down history recall (see
"Command history" below) and slash-command autocomplete (see below).

### Enter to send, Ctrl+J/Alt+Enter for a newline

Plain **Enter** submits the message, same as before. To insert a literal
newline instead — for a multi-line message — use **Ctrl+J** or **Alt+Enter**.
Ctrl+J is the primary binding: it's the raw LF byte, so it works identically
across terminals regardless of whether Shift+Enter/Alt+Enter can even be
distinguished from plain Enter there (many terminals need the kitty-keyboard
protocol for that, which not all support). `ChatInput._on_key` intercepts
`enter` to post a `Submitted` message instead of letting `TextArea` insert a
newline, and intercepts `ctrl+j`/`alt+enter` to insert `"\n"` directly instead
of falling through to `TextArea`'s own bindings.

### Auto-growing box

The box starts at one visible line and grows as you type real newlines (via
Ctrl+J/Alt+Enter, or line-wrapped input from `TextArea`'s own `soft_wrap`),
up to a cap of **10 visible lines** (`_MAX_VISIBLE_LINES`,
`src/pcli/tui/widgets/chat_input.py`) — `_on_text_area_changed` recalculates
`self.styles.height` from the document's line count, clamped to
`[1, 10]`, on every change, and shrinks back down the same way as lines are
removed.

### Paste never grows the box

A **paste is never inserted as real multi-line text**, however large it is —
if it grew the box the same way typed newlines do, pasting something huge
(a stack trace, a whole file) would balloon the input to its 10-line cap
instantly. Instead, a multi-line paste is collapsed to a compact one-line
placeholder like `[Pasted 4 lines]` while you keep composing, and the real
full text is substituted back in only at submit time — a single-line
clipboard is still inserted directly, with no placeholder involved. This
placeholder/substitution logic (`decide_paste`/`PendingPaste`,
`src/pcli/tui/widgets/paste_marker.py`) is shared with `PasteInput`
(`src/pcli/tui/widgets/paste_input.py`), the plain-`Input`-based widget used
for the import-path field in `/sessions`'s import modal — the two widgets
differ in how they actually insert text (`Input.replace()` vs.
`TextArea.replace()`) but share one placeholder/expansion implementation
rather than two diverging copies. On `ChatScreen.on_chat_input_submitted`,
`event.chat_input.consume_pending_paste(text)` runs before any `!`/`!!`/`!!!`/
`/` dispatch, expanding a still-present placeholder back to the original
clipboard text — so what's actually sent to the model and stored in the
session is always the full pasted text, never the placeholder string.
Editing the placeholder away, or pasting again before submitting, just clears
the stale pending state.

Two delivery paths funnel into this same paste logic, since terminals split
paste delivery across them unpredictably:

- **Terminal-intercepted paste** — most terminals (Windows Terminal, xterm,
  GNOME Terminal, ...) intercept the paste gesture themselves (Shift+Insert,
  middle-click, Ctrl+Shift+V) and deliver the clipboard over the
  bracketed-paste channel Textual already has enabled, which Textual turns
  into an `events.Paste` message. `ChatInput` overrides `_on_paste` to apply
  its own logic here instead of falling through to `TextArea`'s built-in
  handler. This is the path that fires in practice on most setups, including
  Windows Terminal.
- **Literal keystroke** — terminals that instead pass Shift+Insert through as
  a plain keystroke leave Textual with nothing bound to it by default, so
  `ChatInput` also adds an explicit **Shift+Insert** binding
  (`action_paste_from_os_clipboard`) that reads the OS clipboard directly via
  the `pyperclip` package, as a fallback for this case. If `pyperclip` can't
  reach a clipboard mechanism (e.g. a minimal Linux setup without
  `xclip`/`xsel`/`wl-clipboard`), an error toast is shown instead of
  crashing; an empty clipboard is a silent no-op.

Both are also distinct from Textual's built-in Ctrl+V, which only reflects
text copied *within* the app (Textual's `App.clipboard` explicitly doesn't
track the OS clipboard) and so does nothing useful for text copied from
outside pcli (a browser, another terminal, an editor). All of `TextArea`'s
other bindings (arrow keys, cursor movement, etc.) are unaffected — Textual
merges a subclass's `BINDINGS` with the parent's rather than replacing them.

### Command history: Up/Down

**Up** and **Down** recall previously-submitted messages, shell-style —
oldest-to-newest walking up, back down toward whatever you'd been composing
before you started browsing history. This only kicks in **while the current
draft has no newline in it**: the moment a draft has a real newline (from
Ctrl+J/Alt+Enter), Up/Down instead move the cursor within that multi-line
text like you'd expect from a normal text box, rather than trying to reason
about soft-wrapped visual rows vs. logical lines
(`ChatInput.action_cursor_up`/`action_cursor_down`,
`src/pcli/tui/widgets/chat_input.py`). Pressing Up from a fresh, empty box
starts at the most recently submitted message; continuing to press Up walks
further back; Down walks forward again and, one press past the newest
history entry, restores whatever draft you had in progress before you
started browsing (`_draft_before_history`) rather than leaving you on the
newest history entry forever. History is populated by
`ChatScreen.on_chat_input_submitted` calling `add_to_history(text)` right
after handling any submission — plain text, `/command`, or `!shell` alike —
and is in-memory only (not persisted across restarts).

The import-path field in `/sessions`'s import modal
(`id="import-path-input"`, `src/pcli/tui/screens/sessions.py`) still uses the
older `PasteInput` (`Input`-based) with the default
`expand_full_paste=False` — there, being a single-value path field rather
than a message box, a multi-line clipboard is truncated to its first line
instead, matching Textual's own `Input._on_paste` behavior, and there's no
auto-grow or history recall since it's a single-line field.

### Slash-command autocomplete

Typing **/** (optionally followed by a partial command name, before any
space) shows a dropdown of matching commands with a one-line description
each, rendered by `CommandSuggestions`
(`src/pcli/tui/widgets/command_suggestions.py`) directly above the input box;
further characters filter it by prefix match on the command name (e.g.
`/comp` matches `compact`), and typing a space once the command name is
complete closes it. **Up**/**Down** move the highlighted suggestion —
taking priority over the Up/Down history recall above whenever the dropdown
is showing, since both key off the same bindings — **Tab** or **Enter**
accepts it, filling the input with `/<command> ` (trailing space, cursor at
the end) *without* sending it, so you can keep typing arguments, and
**Escape** dismisses the dropdown without changing what's typed. The list
driving it (`_SLASH_COMMANDS`, `src/pcli/tui/screens/chat.py`) is a separate,
terser set of one-liners from `/help`'s fuller descriptions — see
[Slash commands](#slash-commands) below for what's available.

## Mouse and clipboard

Scroll-wheel scrolling and click-drag text selection/copy in the message view
work out of the box — none of this is pcli-specific code, it's all Textual
8.2.8 default behavior:

- **Scroll wheel** works because Textual's `Driver.__init__` defaults
  `mouse=True`, and `PcliApp(settings).run()` (`src/pcli/cli.py`) never
  overrides it.
- **Click-drag selection** works because `Widget.ALLOW_SELECT`,
  `Screen.ALLOW_SELECT`, and `App.ALLOW_SELECT` all default to `True` in
  Textual 8.2.8.
- **Ctrl+C / Cmd+C copies the selection** via `Screen.BINDINGS`'s built-in
  `ctrl+c`/`super+c` -> `action_copy_text`, which sends the selected text to
  the terminal over a real **OSC 52** escape sequence
  (`App.copy_to_clipboard`) rather than an in-process-only clipboard — so it
  also works over SSH. `ChatScreen.BINDINGS` doesn't declare a conflicting
  `ctrl+c` of its own (see "Quitting" above), so this reaches Textual's
  built-in handler unobstructed; it's unrelated to Ctrl+C not quitting the
  app — that's a separate `App`-level binding, not something that "uses up"
  Ctrl+C here.

**Caveat:** per Textual's own docstring, OSC 52 "does not work on macOS
Terminal.app" — there's no way to copy via Ctrl+C on that terminal. Fall back
to the terminal's own native selection instead: hold **Shift** while
click-dragging, which (on most terminal emulators) bypasses the app's mouse
capture in favor of the terminal's own copy mechanism.

### Inside tmux or screen

Mouse actions (scroll, click-drag select/copy) can stop working when pcli
runs inside a `tmux` or GNU `screen` session, because the multiplexer sits
between the real terminal and pcli and has to explicitly forward both raw
mouse events and the OSC 52 clipboard sequence through — many default
configs don't. For tmux, add to `~/.tmux.conf`:

```
set -g mouse on
set -g set-clipboard on
```

`mouse on` makes tmux forward click/drag/scroll to the app that requested
mouse tracking (pcli does, by default) instead of consuming them for pane
selection/resizing. `set-clipboard on` makes tmux relay the OSC 52 sequence
through to the real terminal's clipboard instead of swallowing it. Reload
with `tmux source-file ~/.tmux.conf` (or restart the session). Also worth
checking `TERM` inside tmux is something sane like `tmux-256color` — a stale
`TERM` can cause mouse escape codes to be misinterpreted. The local terminal
emulator outside tmux also needs to support receiving OSC 52 itself (most
modern ones do — Windows Terminal, iTerm2, kitty, alacritty, WezTerm — some
don't, by default or at all).

GNU screen's mouse/OSC 52 passthrough support is much weaker and less
reliable than tmux's, with no equivalent high-confidence config to give — if
switching to tmux is an option, that's the more reliable path. Otherwise,
**Shift-drag** for the terminal's own native selection (above) is the most
reliable fallback regardless of multiplexer, since it's handled entirely by
the terminal and never passes through the multiplexer or pcli at all.

## Esc+Esc: cancel turn

Pressing **Escape twice within 0.6 seconds**
(`_ESCAPE_DOUBLE_PRESS_WINDOW_S`, `src/pcli/tui/screens/chat.py:58`) while a
turn is in progress cancels it — both the streaming reply and/or whatever
tool is currently executing as part of that turn, including killing a real
running shell subprocess (`ChatScreen.action_cancel_turn`,
`src/pcli/tui/screens/chat.py:268`, via `self.workers.cancel_group(self,
"agent-turn")`). This is what the `CancelledError`-triggered process-kill
fix in both sandbox backends is for — see
[`sandbox-and-permissions.md`](sandbox-and-permissions.md#restrictedsubprocesssandbox)
— without it a cancelled turn's shell command would keep running in the
background, orphaned.

- **A single Escape press** just shows a toast notification — "Press Esc
  again to cancel the current turn." — rather than a transcript message
  (purely instructional, nothing worth a permanent scrollback record), and
  does nothing else, so a reflexive or stray Escape (e.g. dismissing a
  thought) can't accidentally kill real work.
- **Escape with no turn running** is a silent no-op.
- After cancelling, a system message "Turn cancelled." appears in the chat.
  If one or more follow-up messages had been queued mid-turn (see "Submitting
  while a turn is in progress" above, `ChatScreen._queued_followups`) they're
  dropped rather than sent, and the note says so, pluralized as needed: "1
  queued follow-up message not sent." or "2 queued follow-up messages not
  sent."
- No new keybinding conflicts: plain Escape had no prior binding on the chat
  screen (`ChatScreen.BINDINGS`, `src/pcli/tui/screens/chat.py:72`).

## Ctrl+G: subagent activity panel

Pressing **Ctrl+G** at any time opens a live-updating view of the
currently-running subagent's task and full tool-call history
(`ChatScreen.action_show_subagent_activity`, `src/pcli/tui/screens/chat.py:915`,
bound in `ChatScreen.BINDINGS` at line 175) — a live companion to
[`/subagent`](#slash-commands) below, which only ever prints a one-off,
point-in-time snapshot into the chat transcript.

- **If no subagent is running**, Ctrl+G shows the same message `/subagent`
  shows — "No subagent is currently running." — and opens nothing. Both are
  shown as a toast notification, not a transcript message (trivially
  re-checked by pressing Ctrl+G or retyping `/subagent`), reading the same
  text from the same module-level constant, `_NO_SUBAGENT_RUNNING_MESSAGE`
  (`src/pcli/tui/screens/chat.py:72`), so they can't drift out of sync with
  each other.
- **If a subagent is running**, Ctrl+G pushes `SubagentActivityModal`
  (`src/pcli/tui/screens/subagent_activity_modal.py`), a read-only
  `ModalScreen`. On mount it subscribes to the same shared `ActivityTracker`
  instance `/subagent` reads from (`ActivityTracker.subscribe`,
  `src/pcli/agent/activity.py`) and re-renders on every subsequent tool call,
  using the same `format_subagent_activity` helper `/subagent` uses for its
  static snapshot — so the two commands show identical content, just at
  different moments, and never disagree with each other. `ActivityTracker`
  supports multiple simultaneous subscribers (a list, not a single callback
  slot) specifically so this modal's subscription can coexist with
  `ChatScreen`'s own subscription that keeps the [status bar](#status-bar)'s
  subagent line updated — opening the panel doesn't interrupt that.
- **Full-screen, not a floating box pinned near the top.** Unlike the
  permission prompt, model picker, and ask-question modals — which share
  `align: center top` (pinned to the top of the screen so the chat transcript
  stays visible underneath, rather than centered over it) plus a `width: 70%;
  height: auto; max-height: 80%` rule on `#permission-modal, #import-modal,
  #model-list-modal, #ask-question-modal` — `#subagent-activity-modal` is
  `width: 100%; height: 100%` (`src/pcli/tui/styles/pcli.tcss`), taking over
  the whole window the same way the main chat screen does. This is a
  dedicated view for watching a subagent work, not a quick prompt.
- **A separate title bar sits above the scrollable body.** `compose()` yields
  a `Static` with id `subagent-activity-title` reading "Subagent activity —
  press Esc to return to the main agent's window", styled with
  `text-style: bold` and a `border-bottom: solid $primary` rule
  (`#subagent-activity-title` in `pcli.tcss`), above the `VerticalScroll`
  (`#subagent-activity-scroll`, `height: 1fr`) holding the actual activity
  body.
- **Escape** closes the modal (`SubagentActivityModal.action_close`), same as
  everywhere else Escape closes an overlay — and there's now also a real,
  clickable **Close** button (`Button("Close", id="subagent-activity-close-button")`,
  full-width via `width: 100%` in `pcli.tcss`) at the bottom of the modal,
  wired through `on_button_pressed` to the same `self.dismiss(None)` Escape
  uses. This replaces an earlier version of the modal that only baked a
  plain-text "[Esc] Close" hint into the body — text that looked clickable
  but wasn't; the button now gives Escape a real on-screen equivalent.
- **If the subagent finishes while the modal is still open**, it doesn't get
  yanked shut from under you — the body switches to "Subagent finished —
  nothing more to show." instead, and stays open until you dismiss it
  yourself.
- **Strictly read-only.** If the subagent is currently blocked inside
  `ask_user_question`, the pending question is shown here too, for context —
  but answering it still happens only through the separate
  [ask-question modal](#ask-question-modal), which pops up automatically on
  top when that happens. Textual's modal stack lets both be open at once
  without conflict.

## Gateway errors

When a turn fails against the gateway (`GatewayError`), pcli shows a system
message in the transcript — `Gateway error: <message>` — and logs a full
traceback to `pcli.log` regardless of `--verbose` (see
[`configuration.md`](configuration.md#cli-flags)). `<message>` isn't just the
raw exception text: it now folds in a concrete, actionable hint pointing at
the specific setting to change, wherever the failure maps to something
fixable. The case that prompted this: a local model streaming a reply slowly
enough to exceed pcli's HTTP read timeout used to show a bare "Gateway
error" with nothing else to go on; now it names the effective timeout
currently in effect, suggests a larger value, and says how to set it
(`PCLI_REQUEST_TIMEOUT_S` or `config.toml`) — or, quicker than hand-editing
`config.toml`, [`/timeout <seconds>`](#slash-commands) sets it live from
right inside the TUI, no restart needed — see
[`configuration.md`](configuration.md#local-api-mode) for the related 600s
timeout floor pcli now applies automatically to `--local-api` gateways. The
same "name the setting, suggest a value" pattern
covers a bad/missing gateway API key (HTTP 401/403), rate limiting (HTTP
429, points at `max_retries`), and other gateway-side failures, and applies
everywhere a gateway error can surface — `/models`, `/compact`, `/toolbox
discover` (below), subagent failures, and the `pcli toolbox discover` CLI
command — not just a plain chat turn. `/compact` and `/toolbox discover`
also used to crash uncaught on a gateway failure; both now report the error
as a normal system message instead.

For a plain chat turn specifically, `ChatScreen.on_chat_input_submitted`
appends the user's message to `session.messages` *before* the turn even
starts, unconditionally — so a `GatewayError` doesn't discard it. Users
naturally assume a failed turn means their message was skipped, when in
fact it's still sitting in history and gets resent as context on the very
next turn, alongside whatever's typed next. The `Gateway error: <message>`
notice now says this explicitly, with a bold `**Note:**` (rendered through
Markdown like every other system message, so this is real emphasis, not
just a plain-text label) so it actually catches the eye instead of
blending into the rest of the notice.

A `GatewayError` partway through a plain chat turn is also often exactly a
context-length overflow — an internal round-trip within the turn getting
rejected by the gateway for being too large, sometimes right after the
status bar's live usage display has already shown context crossing 100% on
an earlier round-trip of that same turn. Rather than just reporting the
error and leaving the session stuck over threshold for every subsequent
turn, pcli still attempts the same prune-then-maybe-auto-compact recovery
that runs at the end of a clean turn (see
[Auto-compaction](#auto-compaction) above) right before the error is shown —
safe to attempt unconditionally, since it's a no-op unless context actually
reads as over `auto_compact_threshold`.

## Slash commands

- **`/help`** — prints a static Markdown reference (`_HELP_TEXT`,
  `src/pcli/tui/screens/chat.py`) covering every slash command below, the
  `!`/`!!`/`!!!` shell passthrough tiers, and the Enter/Ctrl+J, Up/Down, and
  slash-command autocomplete input-box behavior — the same information as
  this section and the two below it, condensed for in-app lookup. The input
  box's placeholder text points
  here (`"Ask pcli... (/help for all commands — Enter to send, Ctrl+J for a
  newline)"`) rather than spelling out every command inline.
- **`/sessions`** — opens the session list screen (`SessionListScreen`):
  browse previously stored sessions in a header-labeled table (the last 4
  characters of the session id, title, model, cost, last-updated),
  `Enter` resumes one in a fresh `ChatScreen`, `e` exports the highlighted
  session, `i` prompts for a file path to import, `Esc` goes back. Both this
  screen and normal pcli startup silently prune any never-used (zero
  message) sessions first — see
  [`sessions-and-cost.md`](sessions-and-cost.md#pruning-empty-sessions).
  If the session being resumed (`Enter`) was last run in a different
  directory than the current one (`Session.working_dir`, compared via
  `session/directory_check.py`'s `directory_mismatch()`), a confirmation
  modal warns that relative paths in its history — and any new tool calls —
  will resolve against the current directory instead; `Cancel` returns to
  the list without resuming, `Continue` proceeds as normal. A session with
  no recorded `working_dir` (older sessions, or one imported from another
  machine — see [`sessions-and-cost.md`](sessions-and-cost.md#export--import))
  never triggers this warning.
- **`/export [path]`** — exports the *current* session via `export_session`
  to `path` if given, else `<data_dir>/exports/<session_id>.pcli-session.json`.
  The "Exported session to ..." confirmation is a toast, not a transcript
  message — the exported path is right there in the notification, and the
  file itself is the durable record, not this confirmation.
- **`/toolbox discover <name>`**, **`/toolbox list`**, **`/toolbox remove
  <name>`** — in-TUI equivalents of the `pcli toolbox ...` CLI subcommands;
  see [`toolbox-plugins.md`](toolbox-plugins.md). `discover`/`remove`
  additionally reload the tool registry so newly discovered tools are usable
  immediately. All three share a "Toolbox isn't available (gateway/sandbox
  not set up)." guard, shown as a toast, not a transcript message; a
  dispatcher-level usage hint ("Usage: /toolbox discover... | /toolbox list |
  /toolbox remove...") for an unrecognized form is also a toast.
  `discover`'s own "Discovering '{name}'..." progress ping is a toast too —
  it's superseded moments later by the real outcome — but that outcome
  itself (the failure message or the success summary) stays in the
  transcript as genuine reference content — see also [Gateway
  errors](#gateway-errors) above, which covers `/toolbox discover`'s own
  gateway-failure reporting. `list`'s "No software discovered
  yet..." is a toast; its actual listing when there are discovered tools
  stays in the transcript. `remove`'s "Removed '{name}' from the toolbox."
  success is a toast.
- **`/models`** — with no argument, fetches the gateway's `GET /models` and
  opens a picker screen (`ModelListScreen`) to select interactively; with an
  argument (`/models <model-id>`), sets the model directly without a round
  trip. Either way the choice updates the running session, the status bar,
  and is persisted to `config.toml` (`update_config_file(default_model=...)`)
  so a bare `pcli` picks it up next time. The "Model set to '...'"
  confirmation is a toast, not a transcript message, whether the model was
  set directly by name or picked from the list screen — as are "No gateway
  URL configured. Set PCLI_GATEWAY_URL and restart pcli." and "Gateway
  returned no models." A "Failed to list models: ..." gateway failure is
  unchanged — still a transcript message, same as other real gateway errors
  (see [Gateway errors](#gateway-errors) below).
- **`/compact`** — manually summarizes and archives the oldest conversation
  history right now (see [`tools.md`](tools.md#auto-compaction)), bypassing
  the `auto_compact_threshold` check entirely. If the gateway/sandbox isn't
  set up yet, it shows "Compaction isn't available (gateway/sandbox not set
  up)." instead. Still subject to
  `maybe_compact`'s own "not enough history yet" guard, reporting "Nothing to
  compact yet." if there isn't more history than
  `auto_compact_keep_recent_turns` turns — or, for a session made up of a
  single long-running turn (one user message followed by many internal
  tool-calling rounds), if that turn hasn't yet grown past the fallback
  threshold described in [Auto-compaction](#auto-compaction) below. If
  context is still at or above `auto_compact_threshold` when that first
  attempt finds nothing, `/compact` retries with a smaller
  `keep_recent_turns` before giving up — see
  [Auto-compaction](#auto-compaction) below. Refuses
  to run while a turn is still in progress, showing "Still working on the
  current turn — try /compact again once it's done." instead — just try
  again once it finishes. This
  doesn't affect auto-compaction (below), which always runs sequentially
  after a turn ends anyway. All three of those guards — "isn't available",
  "still working on the current turn", and "Nothing to compact yet." — are
  shown as toasts, not transcript messages, trivially re-checked by rerunning
  `/compact`. A real failure ("Compaction failed: ...") and the actual
  success notice (see [Auto-compaction](#auto-compaction) below) are
  unchanged — both stay in the transcript as genuine, investigable records.
- **`/timeout [seconds]`** — with no argument, reports the current
  `request_timeout_s` (see [`configuration.md`](configuration.md)), plus an
  "(effective: Ns — floored for local-api)" note when the
  [local-api floor](configuration.md#local-api-mode) is currently raising the
  effective timeout above the raw setting. With an argument
  (`/timeout 900`), sets `request_timeout_s` immediately — `GatewayClient`
  reads it fresh on every request, so the new value applies starting with the
  very next gateway call, no restart needed — and persists it to
  `config.toml`, the same way `/models <model-id>` persists `default_model`.
  Rejects non-numeric input and values that aren't greater than 0. Both the
  report and the set-confirmation are shown as a toast notification
  (Textual's built-in `self.notify`) rather than a permanent transcript
  message — quick, glanceable feedback that's trivially re-checked by running
  the command again; most of the settings commands below follow this same
  convention.
- **`/temperature [value|off]`** — view or set `default_temperature` (see
  [`configuration.md`](configuration.md#settings-fields)), the sampling
  temperature sent with each request. With no argument, reports the current
  value, or `"unset (gateway/model default)"` when it's `None` (the default).
  With an argument (`/temperature 0.7`), sets it — any value `>= 0` is
  accepted, including `0` itself, which is a real, deterministic setting
  ("always pick the top token"), not the same thing as unset — persists it to
  `config.toml` via `update_config_file` the same way `/timeout` persists
  `request_timeout_s`, and pushes it live into the running `AgentLoop` via
  `AgentLoop.set_temperature()`, so it applies starting with the very next
  turn (`AgentLoop` threads it through to every `GatewayClient.chat_stream`
  call `run_turn` makes, the same wiring shape as
  [`/max-response-tokens`](#slash-commands)'s `max_tokens`). `off` clears it
  back to unset — restoring "no `temperature` field sent at all" (the
  gateway/model's own default applies) rather than pinning it at some
  concrete number like `0`. This is persisted differently from a numeric
  set: `off` calls `remove_config_keys("default_temperature")` to actually
  delete the key from `config.toml`, instead of `update_config_file`, because
  `update_config_file` deliberately *skips* writing a `None`/`""` value
  (precisely so passing an optional CLI flag/selection through doesn't
  accidentally clear a saved preference) and so has no way to express "delete
  this key and go back to unset" — `remove_config_keys` exists specifically
  to fill that gap. Rejects non-numeric input (other than `off`) and negative
  values. Shown as a toast, not a transcript message, same as `/timeout`
  above.
- **`/budget [amount|off]`** — view or set `max_session_cost_usd` (see
  [`configuration.md`](configuration.md#settings-fields)), a hard cap on this
  session's total spend (`Session.cost.session_total_usd`, which already
  folds in subagent/compaction/memory-extraction spend). With no argument,
  reports the current cap (or `"unset (no cap)"`) plus spend so far. With an
  argument (`/budget 5.00`), sets it — any value `> 0` is accepted — and
  persists it to `config.toml` via `update_config_file`, the same way
  `/temperature` persists `default_temperature`. `AgentLoop.run_turn`
  consults it at the top of every internal round-trip, not just once per
  turn (see [Session cost-budget
  enforcement](sessions-and-cost.md#session-cost-budget-enforcement)), so it
  takes effect starting with the very next round-trip, no restart needed.
  `off` clears it back to unset the same way `/temperature off` does — via
  `remove_config_keys("max_session_cost_usd")` rather than
  `update_config_file`, since the latter deliberately skips writing a `None`
  value rather than persisting a removal. Rejects non-numeric input (other
  than `off`) and values that aren't greater than 0. Shown as a toast, not a
  transcript message, same as `/timeout` above.
- **`/context-limit [tokens]`** — with no argument, reports the context-
  window size pcli currently assumes for the active model (the session's
  model, falling back to `default_model`) — see
  [`configuration.md`](configuration.md#context-limit-detection-and-correction).
  For a handful of backends (LM Studio, Ollama, a LiteLLM proxy, a raw
  llama.cpp server, OpenRouter, vLLM) pcli already tries to fill this in
  automatically on startup — see
  [Automatic context-limit detection](configuration.md#automatic-context-limit-detection)
  — so this command is mainly needed for gateways it can't probe (e.g.
  hosted OpenAI/Anthropic) or to override a wrong guess.
  With an argument (`/context-limit 16384`), sets it and persists it to
  `context_limits.toml`'s `[models]` table — creating the file if needed,
  preserving any other entries already there — the same file manual edits
  already target. The running `ContextLimitTable` is reloaded immediately
  after, so the corrected value applies starting with the very next turn, no
  restart needed. Rejects non-numeric input and values that aren't greater
  than 0. Shown as a toast, not a transcript message, same as `/timeout`
  above.
- **`/max-tool-iterations [n]`** — with no argument, reports the current
  `max_tool_iterations` (default 25), which caps how many tool-call
  round-trips a single turn can make before `AgentLoop.run_turn`'s iteration
  guardrail cuts it off — and, while in
  [local-api mode](configuration.md#local-api-mode), also notes "(currently
  uncapped: local-api mode)", since `is_local_api()` sessions always run
  unlimited regardless of this setting (`_effective_max_tool_iterations`
  returns `None` there). With an argument (`/max-tool-iterations 40`), sets
  it and persists it to `config.toml` the same way `/timeout` persists
  `request_timeout_s`, and pushes the new value live into the running
  `AgentLoop` via `AgentLoop.set_max_tool_iterations()` so it applies
  starting with the very next turn, no restart needed — except in local-api
  mode, where the response says as much ("This session is in local-api mode,
  so it stays uncapped until that changes.") and the value is still saved
  for whenever local-api mode is off. Rejects non-numeric input and values
  that aren't greater than 0. Shown as a toast, not a transcript message,
  same as `/timeout` above.
- **`/artifact-threshold [chars]`** — with no argument, reports the current
  `artifact_threshold_chars` (default 4000) — the tool-output length beyond
  which a result is truncated out of the live conversation and archived to
  the artifact library, retrievable via the `fetch_artifact` tool (see
  `AgentLoop._archive_if_large` and
  [`tools.md`](tools.md#artifact-archiving)). With an argument
  (`/artifact-threshold 8000`), sets it, persists it to `config.toml` the
  same way `/timeout` persists `request_timeout_s`, and pushes the new value
  live into the running `AgentLoop` via
  `AgentLoop.set_artifact_threshold_chars()` so it applies starting with the
  very next tool result, no restart needed. Rejects non-numeric input and
  values that aren't greater than 0. Shown as a toast, not a transcript
  message, same as `/timeout` above.
- **`/max-tool-calls-per-turn [n]`** — with no argument, reports the current
  `max_tool_calls_per_turn` (default 25, `GuardrailsConfig`,
  `src/pcli/permissions/guardrails.py`) — a hard guardrail cap
  `AgentLoop.run_turn` enforces on how many tool calls a single turn may
  dispatch, separate from (and evaluated independently of) the softer
  `/max-tool-iterations` round-trip cap above; once it's hit, further tool
  calls in that turn are denied outright (rather than executed) and the turn
  ends with a "reached the guardrail limit of N tool call(s)" notice. `0`
  means unlimited — a real, intentional value, not treated as invalid — and
  while in [local-api mode](configuration.md#local-api-mode) the report also
  notes "(currently forced unlimited: local-api mode)", since
  `ChatScreen.__init__` already force-copies both this and
  `max_tool_calls_per_minute` to `0` for the whole session there. With an
  argument (`/max-tool-calls-per-turn 10`), sets it and persists it to a new
  `guardrails.toml` file (separate from `config.toml`) via
  `update_guardrails_limits()`, which writes `0` as a real value rather than
  skipping it, and leaves the `[shell]`/`[fs]`/`[python]` tables and any
  other `[limits]` keys untouched. (`update_config_file` — used for
  `config.toml` settings like `/prune-tool-results` below — was itself fixed
  to match this: it now only skips a real `None`/`""`, so a meaningful
  `False`/`0` value, e.g. `prune_tool_results_enabled = False`, is actually
  written instead of being silently dropped as if it were falsy.)
  Outside local-api mode the new value also applies live to the running
  `PermissionManager.guardrails`, taking effect on the next turn; in
  local-api mode it's still saved to `guardrails.toml` for later, but the
  response says the session "stays unlimited until that changes" rather than
  implying it took effect. Rejects non-numeric input and negative values (0
  is allowed). The view/set report is shown as a toast, not a transcript
  message, same as `/timeout` above — unlike the "reached the guardrail
  limit..." notice above, which is a genuine turn-ending event and still
  appears in the transcript.
- **`/max-tool-calls-per-minute [n]`** — same view/set shape as
  `/max-tool-calls-per-turn` above, but for `max_tool_calls_per_minute`
  (default 60) — a sliding 60-second-window rate cap enforced by
  `PermissionManager._within_rate_limit` (`src/pcli/permissions/manager.py`)
  and shared across every tool call the session's `PermissionManager` gates,
  including a subagent's, since it's handed the same instance. Also read
  from and persisted to `guardrails.toml`'s `[limits]` table via the same
  `update_guardrails_limits()`, with the same `0`-means-unlimited semantics
  and the same local-api force-unlimited caveat (reported as "(currently
  forced unlimited: local-api mode)" with no argument, and "stays unlimited
  until that changes" after a set). Outside local-api mode, a new value
  applies live and takes effect on the next tool call rather than the next
  turn, since a rate limit has no per-turn boundary. Rejects non-numeric
  input and negative values (0 is allowed). Shown as a toast, not a
  transcript message, same as `/timeout` above.
- **`/allowed-roots [add|remove] [path]`** — view or live-edit the
  filesystem guardrail's `allowed_roots` list (`GuardrailsConfig.fs_allowed_roots`,
  default: `["."]`, just the working directory) — the `[fs]` counterpart to
  `/max-tool-calls-per-turn`/`/max-tool-calls-per-minute` above for `[limits]`.
  A path outside every `allowed_roots` entry is a hard guardrail deny,
  checked before any permission prompt, so it's never something a user can
  approve their way past by answering a prompt — this command is the actual,
  intended way to widen what `read_file`/`write_file`/`edit_file`/`list_dir`/
  `glob_search`/`grep`/`diff_files`/`apply_patch`/`download_file` can reach.
  See [Guardrails](sandbox-and-permissions.md#guardrails) for the deny path
  this fixes.
  - With no argument, lists the current `allowed_roots` as a bulleted list,
    followed by a usage reminder (`Usage: /allowed-roots add <path> |
    /allowed-roots remove <path>`).
  - **`add <path>`** — appends `path`, persists the full new list to
    `guardrails.toml`'s `[fs]` table via `update_guardrails_fs_allowed_roots()`
    (preserving `deny_paths` and every other table untouched), and
    hot-reloads it into the running `PermissionManager.guardrails` so it
    takes effect immediately, no restart needed. If `path` is already in the
    list, it's a no-op reporting `"'{path}' is already in allowed_roots."`.
  - **`remove <path>`** — same persist-and-hot-reload treatment in reverse.
    If `path` isn't currently in the list, it's a no-op reporting `"'{path}'
    isn't in allowed_roots."`. Refuses to remove the last remaining entry
    (an empty `allowed_roots` would deny every filesystem tool call
    outright), reporting `"Refusing to remove the last allowed_roots entry —
    the agent needs at least one, or every filesystem tool call would be
    denied."` instead.
  - An unrecognized subcommand reports `"Unknown /allowed-roots subcommand:
    '{sub_command}'. Use /allowed-roots, /allowed-roots add <path>, or
    /allowed-roots remove <path>."`.

  Every branch above — including the no-argument listing — is shown as a
  toast, not a transcript message; unlike [`/theme`](#slash-commands)'s
  no-argument listing (below), which stays in the transcript since it dumps
  the full available-themes list, `allowed_roots` is normally short enough to
  fall into the same "glance and maybe retype" category as `/timeout` above.
- **`/prune-tool-results [off|on|<n>]`** — view/toggle/set the automatic
  tool-result pruning pass (`agent/context_pruning.py`'s
  `prune_old_tool_results`, run every turn from `ChatScreen._run_one_turn`
  right after the turn is saved — see [Tool-result
  pruning](#tool-result-pruning) below for the full mechanism). With no
  argument, reports whether pruning is enabled (default: enabled) and the
  current `prune_tool_results_keep_recent_turns` (default 1) — how many of
  the most recent turns' tool results stay verbatim. `off` disables pruning
  entirely (`prune_tool_results_enabled = False`); `on` re-enables it without
  changing `keep_recent_turns`. A positive integer (`/prune-tool-results 3`)
  sets `prune_tool_results_keep_recent_turns` and implicitly re-enables
  pruning if it was off. All three forms persist to `config.toml` the same
  way `/timeout` persists `request_timeout_s`, and apply live starting with
  the very next turn, since `_run_one_turn` reads these settings fresh each
  time — no restart needed. Rejects anything that isn't `off`, `on`, or a
  positive integer (zero and negative values are rejected, with a message
  pointing at `off` instead of `0` to disable pruning). Shown as a toast, not
  a transcript message, same as `/timeout` above.
- **`/subagent`** — shows the currently-running subagent's full detail: its
  task, every tool call made so far (name + arguments, not just the last
  tool name the status bar's second line shows — see [Status
  bar](#status-bar) below), and, if it's currently blocked inside
  `ask_user_question`, the question it's waiting on you to answer
  (`ChatScreen._handle_subagent_command`, reading `ActivityTracker.subagent`
  and its `SubagentActivity.call_log`/`.pending_question` fields,
  `src/pcli/agent/activity.py`). Each call-log entry is shown as `N.
  tool_name(arguments)`, arguments truncated to 200 characters. Reports "No
  subagent is currently running." if `ActivityTracker.subagent` is `None` —
  either none has run yet this session, or the last one already finished
  (only its final text and tool-call count ever reach the main conversation
  — see [`tools.md#spawn_subagent`](tools.md#spawn_subagent)) — shown as a
  toast, not a transcript message (same text, same toast treatment, as
  Ctrl+G's "nothing running" case above). The activity dump itself, when a
  subagent *is* running, is unchanged — still a transcript message, since
  it's genuine reference content worth scrolling back to. Takes no
  argument. For the same information kept live instead of retyping
  `/subagent` for a fresh snapshot, press
  [Ctrl+G](#ctrlg-subagent-activity-panel) instead.
- **`/memory [forget <id>|clear]`** — view pcli's global, cross-session
  memory of you (nature of work, preferences, conversation style, common
  asks — injected into every session's system prompt), remove one entry, or
  clear it all. With no argument, lists every entry grouped by category with
  its short (last-4-char) id, same abbreviation style `/sessions` uses for
  session ids, and an `(explicit)` marker on entries the user asked directly
  to be remembered — rendered as a real Markdown list (`render_memory_list`,
  [`memory.md`](memory.md#the-memory-command)) with each id in a backtick
  code span, rather than the run-on wall of text it used to print. This
  no-arg listing stays in the transcript — reference content worth
  scrolling back to, same as `/toolbox list`'s listing above. Every other
  branch here is a toast, not a transcript message: `/memory clear`'s
  "Cleared all memory entries." confirmation; `/memory forget <id>`'s
  "Forgot: ..." success, its "No memory entry found matching '...'."
  no-match case, and its ambiguous-match case ("'...' matches more than
  one entry — use a longer id."); `/memory forget`'s own no-argument usage
  hint ("Usage: /memory forget <id>"); and an unrecognized subcommand's
  "Unknown /memory subcommand: '...'." `/memory forget <id>` resolves the
  id by suffix match against every entry's full id, rather than guessing,
  before removing it; `/memory clear` wipes everything. Grows automatically from
  what's explicitly asked to be remembered (any session, any length) and,
  once a session gets long enough to auto-compact, from what pcli notices
  worth keeping on its own — see [Auto-compaction](#auto-compaction) below
  and [`memory.md`](memory.md) for the full mechanism.
- **`/max-response-tokens [off|on|<margin>]`** — view/toggle/set the dynamic
  per-request `max_tokens` cap (`compute_max_response_tokens`,
  `src/pcli/cost/context.py`, recomputed fresh before every turn by
  `ChatScreen._run_one_turn` — see [Dynamic response
  cap](#dynamic-response-cap) below for the full mechanism and the bug it
  fixes). With no argument, reports whether it's enabled (default: enabled),
  the configured `max_response_tokens_safety_margin` (default 512 tokens),
  and what `max_tokens` value it would send right now given the session's
  current context usage. `off` disables it (`max_response_tokens_enabled =
  False` — no cap sent, gateway default applies); `on` re-enables it without
  changing the margin. A positive integer (`/max-response-tokens 1000`) sets
  `max_response_tokens_safety_margin` and implicitly re-enables it if it was
  off. All three forms persist to `config.toml` the same way `/timeout`
  persists `request_timeout_s`, and take effect on the next turn (the cap is
  computed once per turn, not live mid-turn). Rejects anything that isn't
  `off`, `on`, or a positive integer (zero and negative values are rejected,
  with a message pointing at `off` instead of `0` to disable the cap). Shown
  as a toast, not a transcript message, same as `/timeout` above.
- **`/rename [name]`** — with no argument, reports the session's current
  title (`Session.derive_title()` — the value shown in `/sessions`'s list).
  With an argument (`/rename my-feature-branch`), sets `Session.title`
  directly to the rest of the line and saves the session immediately
  (`ChatScreen._handle_rename_command`, `src/pcli/tui/screens/chat.py`).
  `derive_title()` prefers an explicitly-set title over the auto-derived
  snippet of the first message, so a renamed session keeps that name in the
  session list even as the conversation moves on. Shown as a toast, not a
  transcript message, same as `/timeout` above.
- **`/theme [name]`** — with no argument, lists every theme registered on
  the running `App` (`self.app.available_themes`) as a bulleted list,
  marking the currently-active one `(active)`. With an argument (`/theme
  nord`), if `name` is a known theme, switches to it live —
  `self.app.theme = name` repaints every CSS design-token-based style
  immediately, no restart — and persists the choice to `config.toml`
  (`update_config_file(ui_theme=name)`, same pattern as `/rename`), so it's
  remembered next launch — this set-confirmation is shown as a toast, not a
  transcript message, same as `/timeout` above. An unknown `name` reports
  "Unknown theme '`name`'." followed by the same available-themes list,
  rather than silently failing (`ChatScreen._handle_theme_command`,
  `src/pcli/tui/screens/chat.py`) — this listing, like the no-argument
  listing above, stays in the transcript rather than a toast, since it's
  reference content worth scrolling back to. Available themes are Textual's
  own builtins (`textual-dark`, `textual-light`, `gruvbox`, `nord`, `dracula`,
  `monokai`, `catppuccin-*`, `solarized-*`, `tokyo-night`, `flexoki`,
  `rose-pine-*`, `atom-one-*`, `ansi-*`, ...) plus five pcli-specific
  themes approximating classic bundled vim colorschemes not already
  covered by Textual's builtins — `vim-desert`, `vim-torte`,
  `vim-evening`, `vim-elflord`, `vim-slate` (period-appropriate
  approximations, not byte-exact ports — vim's terminal-256-color palette
  model doesn't map 1:1 onto Textual's primary/secondary/accent/surface/panel
  tokens — `src/pcli/tui/themes.py`). These five are registered on startup
  (`PcliApp.on_mount`) alongside Textual's builtins, then `Settings.ui_theme`
  is applied as the initial theme if it names a registered one — see
  [`configuration.md`](configuration.md#settings-fields).
- **`/plan`** / **`/build`** — toggle plan mode: a restricted mode for
  investigating and proposing an approach without the model being able to
  make any changes. See [Plan mode](#plan-mode) below for the full behavior.

Any other `/word` shows a toast notification, "Unknown command: /word",
rather than a transcript message — a mistyped command isn't worth a
permanent scrollback record.

## Plan mode

`/plan` enters plan mode; `/build` exits it back to normal ("build") mode
(`ChatScreen._set_plan_mode`, `src/pcli/tui/screens/chat.py`). While active,
only read-only/exploration tools are available to the model — everything
that writes, edits, or executes is denied. Running the command already
matching the current mode (e.g. `/plan` while already in plan mode) is a
no-op that just reports "Already in plan mode."

Allowed in plan mode (every tool with `plan_mode_safe=True`,
`src/pcli/tools/base.py`'s `ToolSpec.plan_mode_safe` field): `read_file`,
`list_dir`, `glob_search`, `grep`, `search_python`, `inspect_python_module`,
`fetch_artifact`, `read_background_output`, `write_todos`, `record_decision`,
`spawn_subagent`, `web_fetch`, `web_search` (pcli's only two tools with real
internet access — read-only, no local mutation), plus the registered agent
tools (`explore_codebase`, `explore_files`, `explore_logs`, `deep_research`,
and any `register_agent_tool`-defined ones marked `plan_mode_safe`) — see
[`tools.md`](tools.md) for what each does. Everything else — `write_file`,
`edit_file`, `run_shell`, `run_shell_background`, `stop_background_process`,
`pip_install`, `call_python`, `register_toolbox_tool`, `register_agent_tool` itself,
`write_documentation`, `verify_computation`, `data_analysis` (all three need
write/execute tools plan mode blocks entirely, so the whole tool is
unavailable), and any toolbox-discovered tool — is unavailable while plan
mode is active.

Three independent layers enforce this, deliberately redundant (defense in
depth) rather than relying on any single one:

1. **Tool registry filtering** — `ChatScreen._effective_tool_registry`
   returns `self._tool_registry.filtered(lambda t: t.plan_mode_safe)` while
   plan mode is on, so the model never even sees a disallowed tool in its
   tool list. This is the primary mechanism.
2. **Dispatch-time backstop** — `AgentLoop._dispatch_tool_call`
   (`src/pcli/agent/loop.py`) independently checks `ctx.plan_mode and not
   tool.plan_mode_safe` and returns "Denied: not available in plan mode." if
   so, regardless of what registry happens to be wired up — protecting
   against a stale registry or a hallucinated tool call that somehow reached
   dispatch. `ToolContext.plan_mode` (`src/pcli/tools/base.py`) carries the
   current mode down into every tool call.
3. **Per-turn prompt reinforcement** — while plan mode is active, each turn
   gets an ephemeral `_PLAN_MODE_REINFORCEMENT` system message appended
   (`src/pcli/tui/screens/chat.py`) reminding the model it's in plan mode and
   should investigate/propose rather than act or try to route around the
   restriction. This is injected fresh per turn and **never persisted** to
   `session.messages`, so it can't drift out of sync via compaction and
   doesn't pollute exports/resumption.

Toggling either command swaps the live `AgentLoop`'s tool registry
immediately via `AgentLoop.set_tool_registry` — no restart, and it takes
effect starting with the very next turn. The status bar shows a `[PLAN
MODE]` tag at the start of its first line while active (`StatusBar.plan_mode`
reactive, `src/pcli/tui/widgets/status_bar.py`) — see
[Status bar](#status-bar) below.

## Auto-compaction

Compaction can also fire automatically and transparently, with no command
needed: at the end of every turn, once the session is saved, if
`auto_compact_enabled` is on (default) and context usage has reached
`auto_compact_threshold` (default 80% of the model's context limit),
`ChatScreen` runs the same summarization `/compact` triggers, reusing the
"Working..." spinner on the status bar for the whole of `_run_compaction` —
not just the summarization call, but also the tool-invocation bookkeeping,
the system notice, the session save, and the memory-extraction pass below,
so the indicator doesn't drop and then reappear across that gap.

This check isn't limited to a turn that completes cleanly: a turn that
instead fails with a [gateway error](#gateway-errors) still gets the same
recovery attempt right before the failure is reported — see below.

When it fires, a short system notice appears in the transcript (e.g.
"Compacted N earlier message(s)... archived as artifact_id='art_...'"); the
actual summary text that replaces the compacted messages is stored as a
`role="system"` message and, like the leading system prompt, is never
rendered into the message view. See
[`configuration.md`](configuration.md#auto-compaction) for the three
settings involved.

That summary is more than free-form prose from the LLM: alongside what the
user asked for, what's been done, and any assumptions made along the way (and
why), it always carries a verbatim, structured snapshot of the session's
actual decisions (with rationale) and current todo list, appended straight
from `Session.decisions`/`Session.todos` rather than reconstructed by the
summarizing model from the raw transcript. Because that part isn't up to the
summarizer's own prose, it can't be lost or garbled the way free-form
narrative can be in a lossy re-summarization pass. See
[`tools.md`](tools.md#auto-compaction) for the exact mechanism and section
format.

Eligibility is normally based on counting real user-typed turns (`/compact`
above and `auto_compact_keep_recent_turns`), but that alone can't see any
structure inside a single long-running turn — one user instruction followed
by many internal tool-calling rounds, the shape of an autonomous task. Such a
turn has only one user-turn boundary for its entire life no matter how large
it grows, so `agent/compaction.py`'s `compaction_cutoff` helper falls back to
a finer per-round boundary (every non-`tool`-role message) once a single turn
has grown past 20 round trips, keeping at least that many of the most recent
rounds verbatim. This previously meant a large single-turn conversation could
never be compacted at all — `/compact` would report "Nothing to compact yet."
no matter how full the context got — and now can be, once it's grown large
enough. An ordinary short turn is unaffected either way.

The `auto_compact_keep_recent_turns` window itself is a soft guarantee, not
an absolute one: a real reported bug had a session auto-compact once, then
hit three consecutive auto-continue retries (see [Auto-continue on truncated
responses](#auto-continue-on-truncated-responses) below — each synthetic
`"Continue."` message is its own turn boundary), leaving nothing older than
the protected window left to summarize even though the status bar still
showed context 100% full, so `/compact` reported "Nothing to compact yet."
instead of freeing any room. If the initial attempt at the configured
setting finds nothing eligible but usage is still at or above
`auto_compact_threshold`, `ChatScreen._run_compaction` now retries with a
progressively smaller `keep_recent_turns` (down to `0`) until one finds
something to compact, and the notice says so explicitly. See
[`configuration.md`](configuration.md#auto-compaction) for the full
behavior.

A successful compaction — automatic or `/compact` — also triggers a
**memory-extraction** pass: pcli reviews the transcript that was just
archived for anything durable and worth remembering about the user across
future sessions, and persists it via the same `remember` tool an explicit
"remember this" uses. This is silent and best-effort (a failure here never
shows up as a turn failure) and never fires without a compaction to
piggyback on. See [`memory.md`](memory.md#autonomous-extraction-derived) for
the full mechanism, and [`/memory`](#slash-commands) above for viewing or
clearing what's been remembered.

## Tool-result pruning

Distinct from auto-compaction above, and much lighter weight: at the end of
every turn — right after the session is saved, *before* the auto-compact
threshold check — `ChatScreen._run_one_turn` calls
`agent/context_pruning.py`'s `prune_old_tool_results` if
`prune_tool_results_enabled` is on (default: on). This is a purely mechanical
pass with **no LLM call involved** — it doesn't summarize anything, it just
shrinks old tool-role message content:

- Any tool-role message older than the most recent
  `prune_tool_results_keep_recent_turns` turns (default 1 — deliberately
  tighter than auto-compaction's `auto_compact_keep_recent_turns` default of
  2, so pruning has something to do before compaction's own threshold is ever
  reached) has its full content archived via the same `ArtifactStore`
  mechanism used by [artifact archiving](tools.md#artifact-archiving) and
  [auto-compaction](#auto-compaction), then replaced in place with a short
  placeholder:

  ```
  [Pruned tool result (12,345 chars) to save context. Purpose: checking whether pdftotext is installed. Call fetch_artifact(artifact_id='art_...') if you need it.]
  ```

  The `Purpose: ...` sentence only appears if the original tool call included
  the optional `purpose` argument (see [`tools.md`](tools.md#the-purpose-argument))
  — `prune_old_tool_results` finds it by scanning the session's assistant
  messages for the matching `tool_call_id` and re-extracting `purpose` from
  that call's original arguments JSON, which is left untouched even after
  pruning.
- Idempotent: once a tool-role message is pruned, `Message.pruned_artifact_id`
  is set on it, so a later turn's pruning pass skips it rather than
  re-archiving (and duplicating) the same content.
- Turn-boundary-safe: it reuses the same `compaction_cutoff()` helper
  auto-compaction uses (see [Auto-compaction](#auto-compaction) below), so it
  only ever operates on complete turns/rounds and never separates an
  assistant `tool_calls` message from its matching tool-result message.
- The archived content is retrievable exactly like any other artifact: call
  `fetch_artifact(artifact_id='...')` with the id from the placeholder.

Both `prune_tool_results_enabled` and `prune_tool_results_keep_recent_turns`
persist to `config.toml` and can be viewed or changed live with
[`/prune-tool-results`](#slash-commands) (unlike auto-compaction's three
settings, which are env-var/config.toml-only with no slash command) — see
[`configuration.md`](configuration.md#tool-result-pruning) for the full
settings reference.

## Auto-continue on truncated responses

A real reported bug: a turn's final response — the one with no tool call,
which normally means the model is done — can instead be a response that got
cut off by the token/response-length cap mid-generation. The model's last
message looked like "Let me implement. Starting with embedding_store.py:"
with no tool call following, and pcli previously had no way to tell that
apart from a deliberate, complete stop, so the turn just silently ended
there with genuinely unfinished work — the user had to notice and type
"continue" themselves to get it moving again.

`AgentLoop.run_turn` (`src/pcli/agent/loop.py`) now tracks the `finish_reason`
of the response that ends a turn's tool-calling loop (from the `FinishEvent`
`llm/streaming.py` already parsed but nothing previously consulted) and sets
`TurnCompleteEvent.response_truncated` when it's `"length"` or `"max_tokens"`
(`_TRUNCATION_FINISH_REASONS` — the two finish-reason spellings observed
across OpenAI-compatible gateways for "cut off by the token cap," as opposed
to `"stop"`, a deliberate finish). This is a separate concept from
`terminated_early` (see [`configuration.md`](configuration.md#local-api-mode)
for that field's own subagent-facing documentation): `terminated_early`
means a guardrail like `max_tool_iterations` cut off an
otherwise-healthy turn, while `response_truncated` means the model's own
response got cut off mid-sentence by the token cap. The two are independent
and either can be true regardless of the other.

`ChatScreen._run_one_turn` checks `response_truncated` **after** the
tool-result pruning and auto-compaction logic above have already run for
the turn — deliberately, so that if context was tight enough to cause the
truncation in the first place, the retry gets a genuine chance at more
headroom instead of immediately hitting the same wall again. When it fires:

- A system notice appears in the transcript: "Response was cut off by the
  token limit before finishing — continuing automatically (N/3)."
- A synthetic `"Continue."` user-role message is appended to the session —
  the exact same fix a user would type manually — and the existing
  `_has_queued_followup` boolean (unrelated, despite the similar name, to
  `_queued_followups` — the list that holds a *user's own* messages typed
  while a turn is in progress, see "Submitting while a turn is in progress"
  above) is set to trigger another turn automatically via the same
  `_stream_response` loop. No new turn-loop mechanism was added. Each
  synthetic `"Continue."` message is its own turn boundary
  like any real user message, so several in a row can concentrate inside
  auto-compaction's protected `auto_compact_keep_recent_turns` window — see
  [Auto-compaction](#auto-compaction) above for what happens when that
  window itself ends up too full to leave anything older to compact.

This is capped at `_MAX_CONSECUTIVE_AUTO_CONTINUES = 3` consecutive
attempts — a hardcoded constant in `chat.py`, not a `Settings` field
(deliberately, the same way `_MAX_IDENTICAL_TOOL_CALL_REPEATS` in the same
module is a hardcoded safety backstop rather than user-configurable) — so a
model that keeps hitting the limit no matter what can't loop forever.
`ChatScreen._consecutive_truncations` tracks the current streak and resets
to `0` whenever a turn completes without truncation, or the user sends a
real message of their own (`on_chat_input_submitted`). Hitting the cap shows
a different notice instead of continuing again: "Response was cut off by the
token limit 3 times in a row — stopping automatic continuation rather than
risk a runaway loop. Try /max-response-tokens to raise the response-length
cap, or just say "continue" to keep going manually." — pointing at
[`/max-response-tokens`](#slash-commands) (see [Dynamic response
cap](#dynamic-response-cap) below) as one way out.

Both notices are message-view-only, like compaction's own "Compacted N
message(s)..." notice above — never persisted to `session.messages`. This
behavior is specific to the interactive `ChatScreen` turn loop; a subagent's
`TurnCompleteEvent.response_truncated` isn't consulted by `spawn_subagent`
or the `make_agent_tool` family (`_nested_agent.py` only reads
`terminated_early`), so a subagent whose final response gets cut off this
way is not auto-continued — it's simply reported back as whatever partial
text it produced.

`pcli run` (`agent/headless.py`) replicates this same auto-continue
mechanism outside the TUI, same 3-attempt cap — see
[`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md#whats-different-from-the-tui).

## Dynamic response cap

Auto-compaction and tool-result pruning above both react to context that's
already accumulated in history, checked *between* turns. Neither has a
checkpoint *inside* a single response while it's still generating — so a
turn that ends comfortably under `auto_compact_threshold` gives compaction no
reason to run, and the very next turn's response can then, all by itself,
generate enough tokens to consume the entire remaining context window before
anyone gets a chance to react. That's exactly what happened in a real
debugged session: a turn ended 69% full (under the 80% default threshold),
and the next turn's response generated 4,663 tokens in a single ~100-minute
generation, consumed the remaining ~31% of the window by itself, and got
hard-truncated mid-stream by the gateway's own context ceiling.

The dynamic response cap is a tighter-grained, complementary safeguard for
exactly this gap — it does not replace auto-compaction or tool-result
pruning, which still run as described above. If `max_response_tokens_enabled`
is on (default: on), `ChatScreen._run_one_turn` computes a `max_tokens` cap
fresh before every turn and applies it to the outbound request
(`compute_max_response_tokens`, `src/pcli/cost/context.py`, plumbed through
`AgentLoop.set_max_response_tokens` into `GatewayClient.chat_stream`/
`collect`'s `max_tokens` parameter):

```
max_tokens = context_limit - last_known_used_tokens - max_response_tokens_safety_margin
```

- `context_limit` and `last_known_used_tokens` come from the same
  `current_context_usage` (`cost/context.py`) that auto-compaction's own
  threshold check already uses — pcli has no local tokenizer for a generic
  gateway (see that module's docstring), so this is exactly as accurate as
  pcli's other context-usage decisions, no more, no less.
- `max_response_tokens_safety_margin` (default 512 tokens) is headroom kept
  below the limit on top of the cap itself.
- **It's a dynamic cap, not a fixed number** — it shrinks every turn as
  `last_known_used_tokens` grows, tracking however much of the window is
  actually left.
- **Silently skipped (no `max_tokens` sent, gateway default applies) in two
  cases**: the first turn of a session (no prior usage yet to compute
  headroom from), or once the computed headroom is already `<= 0` (the
  window's essentially full — by that point auto-compaction should already
  have intervened).
- Computed once per user-submitted turn, not recomputed live mid-turn: a
  turn's own tool-call round-trips reuse the same cap rather than shrinking
  it further as they add their own usage — a deliberate simplification
  given the bug this fixes (one very long response, not a many-tool-call
  turn). See `AgentLoop.set_max_response_tokens`'s docstring
  (`src/pcli/agent/loop.py`) for the exact reasoning.

Both settings persist to `config.toml` and can be viewed or changed live with
[`/max-response-tokens`](#slash-commands) — see
[`configuration.md`](configuration.md#dynamic-response-cap) for the full
settings reference.

## Shell passthrough: `!command`, `!!command`, and `!!!command`

Typing `!<command>` runs it directly via `run_passthrough_command`
(`src/pcli/tui/shell_passthrough.py`) and prints `$ command` followed by
stdout/stderr and `[exit_code=N]`. `!!<command>` runs it the same way but
prints `(output hidden)` instead of the result — useful for commands you
don't want cluttering the transcript (e.g. clearing a screen, opening
something).

This is **deliberately unsandboxed and unrestricted**: it runs in the real
working directory with the real environment, exactly like a normal terminal.
It bypasses the `Sandbox` abstraction, the guardrail/permission system, *and*
the LLM entirely — nothing about it touches `Session` state, the artifact
library, or the model's context window. It exists purely so you can inspect
things without leaving pcli, on the reasoning that these protections exist to
constrain the LLM, not the human at the keyboard. Output is capped at 50,000
characters and the default timeout is 120s.

### `!!!command`: real interactive terminal handoff

`!`/`!!` pipe the child process's stdout/stderr back into pcli, so anything
that needs a genuine TTY — password prompts, REPLs, editors like `vim`, `ssh`
sessions — doesn't work through them (Textual already owns pcli's own
stdin). `!!!<command>` is a third, separate tier for exactly that case:
`ChatScreen._run_interactive_shell` (`src/pcli/tui/screens/chat.py`) hands
the real terminal to the command instead of capturing it.

It's checked *before* the `!`/`!!` check in `on_input_submitted`, and is
otherwise fully additive — `!`/`!!` behavior is unchanged. Implementation-wise
it's also a plain **synchronous** method rather than a `@work` worker (unlike
every other shell/command handler in `chat.py`): it wraps a blocking
`subprocess.run(command, shell=True, cwd=..., check=False)` inside Textual's
`App.suspend()`, a context manager that stops the app reading/writing the
terminal for the duration of the `with` block and hands control back to the
OS, restoring pcli's own terminal mode when the block exits. Since
`App.suspend()` itself blocks synchronously, there's nothing useful the event
loop could do concurrently anyway.

Behavior differs from `!`/`!!` everywhere interactivity matters:

- **No timeout** (`!`/`!!` default to 120s, `DEFAULT_TIMEOUT_S`) and **no
  output capture or truncation** (`!`/`!!` cap output at 50,000 characters) —
  the command owns the real terminal directly, so the user has the same
  direct control (e.g. Ctrl+C) as any normal terminal session.
- A `shell`-role message is shown before handoff — `→ Handing off terminal
  to: command` — and another after the command returns — `$ command  (ran
  interactively) [exit_code=N]`.
- Typing `!!!` with nothing after it shows a usage toast instead of doing
  anything, rather than a transcript message.
- If the environment doesn't support suspending
  (`textual.app.SuspendNotSupported` — e.g. not supported in Textual Web), a
  toast is shown instead of crashing: "Interactive shell handoff isn't
  supported in this terminal environment." — also not a transcript message.

Like `!`/`!!`, `!!!` bypasses the LLM, the sandbox, permissions, and
session/artifact recording entirely. It's documented in the `/help` output
(see [Slash commands](#slash-commands) above) rather than spelled out in the
input box's placeholder text, which just points at `/help` for the full
command/shell-passthrough reference.

## Permission prompts

When a tool call needs permission and isn't already covered by a remembered
grant, `PermissionPromptModal` (`src/pcli/tui/screens/permission_modal.py`)
pops up showing the tool name, its arguments (truncated past 800 chars), and
a risk description. Four buttons:

- **Allow Once** — runs this one call, remembers nothing.
- **Allow for Session** — runs it, and auto-allows the same tool name for the
  rest of this process's lifetime (in-memory only, not persisted).
- **Allow Always** — runs it, and persists an "always allow" grant to
  `permissions.json` so future sessions skip the prompt for this tool.
- **Deny** — blocks this call; the model sees `"Permission denied: denied by
  the user."`

Grants are keyed by tool name only (not by specific arguments) — allowing
`run_shell` once for a given command allows it for any command afterward,
for the remainder of the granted scope. See
[`sandbox-and-permissions.md`](sandbox-and-permissions.md) for the full
decision flow (guardrails still apply and can override an "allow"), including
the other reason strings the model sees for a guardrail, rate-limit, or
remembered-grant denial.

## Ask-question modal

The `ask_user_question` tool ([`tools.md`](tools.md#ask_user_question)) has
its own modal, distinct from the permission prompt above: `AskQuestionModal`
(`src/pcli/tui/screens/ask_question_modal.py`) pops up showing the model's
question text, and blocks the turn until you answer — same "pauses execution
until you respond" shape as `PermissionPromptModal`, but for a free-form
question/answer rather than an allow/deny/remember decision. Unlike a
permission-gated tool, this one is read-only with no side effects
(`needs_permission=False`), so there's no risk description or Allow/Deny
choice to make — you're just answering, not authorizing anything.

If the model supplied `options`, one button is rendered per suggested
answer, clicking it dismisses the modal with that value. A free-text input
field is *always* shown too, regardless of whether options are given, so a
suggested option never forecloses a different typed answer — typing
something and pressing Enter dismisses the modal with that text instead.
Wired into `ChatScreen` the same way the permission-prompt `ask` closure is:
an `ask_question` closure is created per turn in `_run_one_turn`, stored as
`self._current_ask_question`, and threaded into the tool call via
`ToolContext.ask_question` (`src/pcli/tools/base.py`) — a separate callback
from `ToolContext.ask` (permission decisions only, a fixed allow/deny/
remember-scope shape), since this one is a free-form question/answer.

**Called from inside a subagent**, the question text shown in the modal is
automatically prefixed with what that subagent has been working on — its
task and the 5 most recent tool call names — since otherwise you'd see a
bare, context-free question with nothing to relate it to (a subagent's own
intermediate work never enters the main conversation; see
[`tools.md#spawn_subagent`](tools.md#spawn_subagent)):

```
[This question is from a subagent working on: "investigate failing test" —
3 tool call(s) so far, most recent: read_file, grep, run_shell. Use
/subagent for the full detail.]

<the model's actual question>
```

This happens automatically (`_ask_user_question`,
`src/pcli/tools/builtin/ask_tool.py`) — no user action needed. `/subagent`
(see [Slash commands](#slash-commands) above) is the on-demand way to see
the subagent's complete tool-call history instead of just the last 5 names.

## Tool calls and results

Before a call's result is known, `ChatScreen._run_one_turn` (on a
`tool_start` stream event) calls `MessageView.add_tool_call(tool_name,
arguments_json, purpose=...)` (`src/pcli/tui/widgets/message_view.py`) to
show a "call" preview. This used to just dump the raw, single-line arguments
JSON verbatim (so a multi-line `write_file` body showed literal `\n`
characters); it now parses the arguments and formats them:

- If the model included the optional `purpose` argument on this call (see
  [`tools.md`](tools.md#the-purpose-argument)), it's shown in italics on its
  own line, and is never mixed into the argument listing/highlighting below
  it (no more double-showing it inline *and* appended after a `#`, like an
  older version of this doc described).
- Tools with a known "code" argument — `write_file`'s `content`,
  `run_shell`/`run_shell_background`'s `command`, `edit_file`'s `new_string`
  (`_CODE_ARG_BY_TOOL`) — get that argument rendered as a real, multi-line,
  syntax-highlighted `rich.syntax.Syntax` block instead of an escaped JSON
  string. The language is guessed from the call's `path` argument when
  present (`Syntax.guess_lexer`), else a per-tool default (`bash` for the two
  `run_shell*` tools, `text` otherwise). Any other arguments on the same call
  (e.g. `path`, `timeout_s`) print as a compact dim `key=value, key=value`
  summary line above the code block.
- `edit_file`'s **replace mode** (a call with both `old_string` and
  `new_string`) is special-cased ahead of the generic `_CODE_ARG_BY_TOOL`
  lookup above, since it has two code arguments: `old_string` and
  `new_string` each render as their own labeled `Syntax` block — `-
  old_string` in bold red, `+ new_string` in bold green — using a lexer
  guessed from `old_string` (and the call's `path`, if given). **Insert
  mode** (`insert_after_line` + `new_string`, no `old_string`) has only one
  code argument, so it falls through to the generic `_CODE_ARG_BY_TOOL` path
  above and gets the same single-block treatment as `write_file`'s `content`.
  **Delete mode** (`delete_start_line`/`delete_end_line`) has no big text
  payload at all, so it falls through further to the generic pretty-JSON
  rendering below.
- Every other tool (`read_file`, `grep`, `list_dir`, toolbox tools, agent
  tools, ...) falls back to pretty-printed, indented JSON
  (`json.dumps(..., indent=2)` through `Syntax(..., "json", ...)`) rather
  than the old single-line raw string — still more readable even without a
  dedicated code field.
- A call with no arguments at all shows `(no arguments)`.

For example, a small `read_file` call still renders inline, uncollapsed:

```
→ read_file
checking whether pdftotext is a declared dependency
{
  "path": "setup.py"
}
```

**Large calls collapse behind a click**, the same pattern used for tool
*results* below: `_format_tool_call_body` measures the char length of what's
actually formatted (the code/JSON body shown, not the raw arguments JSON
string), and if that exceeds `_LARGE_TOOL_CALL_THRESHOLD_CHARS` (500 chars),
the whole call — purpose line plus formatted body — is wrapped in a Textual
`Collapsible`, collapsed by default, with CSS class `tool-call-collapsible`
(`src/pcli/tui/styles/pcli.tcss` — same warning-colored left border as
`.tool-result-collapsible`) and titled:

```
→ write_file(...) — 1,234 char(s)
```

Click the title (or focus it and press Enter/Space) to expand and see the
full formatted content. Small/common calls — most `read_file`, `grep`,
`list_dir` calls — stay visible inline as before, styled with the existing
`message message-tool` classes.

Each tool call's result (`MessageView.add_tool_result`,
`src/pcli/tui/widgets/message_view.py`) renders as a Textual `Collapsible`,
collapsed by default, titled:

```
✓ read_file — 1,234 char(s)
```

(`✗` in place of `✓` when `chunk.is_error`, which also adds an `error` CSS
class giving the collapsible's left border the `$error` color instead of
`$warning` — see `.tool-result-collapsible`/`.tool-result-collapsible.error`
in `src/pcli/tui/styles/pcli.tcss`). Click the title (or focus it and press
Enter/Space, standard Textual `Collapsible` interaction) to expand it and see
the full output. This replaced the old fixed 2000-character view-layer
preview, which used to cut the body short even when the result wasn't large
enough to trigger the model-facing artifact archiving described in
[`tools.md`](tools.md#artifact-archiving) — now the Collapsible always shows
all of `chunk.output` (the same string the model itself received for that
tool call, already-archived-and-truncated if it was over the artifact
threshold).

Body formatting (`_format_tool_output`) depends on the content: output that
starts with `{`/`[` and parses as JSON is pretty-printed and
syntax-highlighted via `rich.syntax.Syntax`; everything else is rendered
verbatim as plain monospace text (`rich.text.Text`), deliberately *not*
Markdown, since tool output routinely contains underscores/asterisks/etc.
that Markdown would misinterpret.

### Reloading/resuming a session

Reopening a session — via `/sessions`' `Enter` or `pcli --resume <id>` —
rebuilds the visible transcript from `session.messages` in
`ChatScreen._replay_message_history` (`src/pcli/tui/screens/chat.py`, called
from `on_mount`), using these *same* `add_tool_call`/`add_tool_result`
helpers rather than a separate, simpler reconstruction. Practically, that
means a reloaded session looks like the original conversation: an
assistant turn that only made tool calls (no text reply) still shows its
`add_tool_call` preview(s), each tool-role message renders as the real
syntax-highlighted/collapsible result described above (not a flat,
hard-truncated line), and a mid-conversation `system`-role message —
notably a compaction summary, which literally replaces the turns it
summarized in `session.messages` itself (see
[Auto-compaction](#auto-compaction) below) — is shown too, not silently
dropped. Each tool-role message is correlated back to the specific
assistant `tool_call` that produced it via `tool_call_id`, so a
`record_decision` call replays as the same always-visible notice a live
turn shows — see [Decision log](#decision-log) below — instead of a raw
tool-result Collapsible. The leading system-prompt message (index 0) is
still skipped either way, exactly as before.

One gap is unavoidable: whether a given past tool call actually errored
isn't retained on `Message` itself — only on the corresponding
`Session.tool_invocations` entry's `status`, which (unlike
`session.messages`) is never pruned or compacted, so it can no longer be
reliably matched back to a specific reconstructed message once either has
happened. Every tool result shown on reload therefore renders as if it
succeeded — no `✗`, no red `.tool-result-collapsible.error` border — even
if the original call actually failed. This is purely a display gap:
`session.messages` itself, what actually gets resent to the LLM on the
next turn, is unaffected either way.

## Model reasoning ("Thinking")

Some models — particularly local "reasoning" models served through an
OpenAI-compatible gateway (e.g. LM Studio) — stream their internal
chain-of-thought separately from their actual reply. When the backend
provides this, pcli shows it as its own panel in the message view, titled:

```
🤔 Thinking — 842 char(s)
```

collapsed by default, the same one-shot-render/click-to-expand pattern as the
tool-result Collapsibles above, styled with a muted left border
(`.reasoning-collapsible`) to keep it visually distinct from tool results and
the actual reply. It appears once a real reply or tool call follows it, or at
the end of the turn if neither does. It's purely something to look at —
unlike the assistant's actual reply, this reasoning text is never sent back
to the model on later turns and isn't part of the conversation history.

### When a turn produces no reply at all

Occasionally a reasoning model spends its whole response budget "thinking"
without ever producing a final answer or tool call. This used to leave a
completely empty, silent assistant bubble with nothing to indicate what
happened. Now pcli shows a clear notice instead: "The model didn't produce a
reply or tool call this turn (see "Thinking" above). Try again, or ask
something more focused." (the parenthetical is omitted if no reasoning panel
was shown either). If you see this, it usually means the model ran out of
room to answer — try again, or rephrase as a more focused ask.

The same notice also checks for a more specific, diagnosable cause:
`looks_like_context_ceiling` (`src/pcli/cost/context.py`, see
[`configuration.md`](configuration.md#context-limit-detection-and-correction))
looks at the session's own turn-by-turn token usage for the fingerprint of a
real, gateway-enforced context ceiling — independent of whatever limit pcli
itself is currently assuming for the model. When it fires, the notice
instead reads: "This looks like it may have hit the model's real context
limit — pcli is currently assuming N tokens for '<model>', which may be
wrong. Try /context-limit <tokens> to correct it (so auto-compaction can
kick in), or /compact to free up space now." — pointing straight at
[`/context-limit`](#slash-commands) as the fix. This is the reactive
backstop for exactly the case
[automatic context-limit detection](configuration.md#automatic-context-limit-detection)
is meant to prevent proactively at startup — you'll typically only see this
notice for a gateway/model that detection couldn't reach in the first place.

This subsection covers a turn that produced *no* text at all. A related but
different case — some text was produced, just cut off mid-sentence by the
token cap before a tool call could follow — is handled separately (and
proactively retried rather than just flagged with a notice); see
[Auto-continue on truncated responses](#auto-continue-on-truncated-responses)
above.

## Collapsible slash-command and shell output

Long `system`-role messages (this covers most of the reference-style
slash-command output that still lands in the transcript — `/help`,
`/memory`, `/subagent`, `/toolbox list`, `/theme`'s no-argument theme
listing, etc. — the quick "glance and maybe retype" config-view/confirmation
commands like `/timeout` now show as toast notifications instead, see
[Slash commands](#slash-commands) above) and `shell`-role messages
(`!command`/`!!command` output — see [Shell
passthrough](#shell-passthrough-command-command-and-command) above) are
wrapped in a Textual `Collapsible` once the text reaches
`_COLLAPSIBLE_MESSAGE_THRESHOLD_CHARS` (300 characters) —
`MessageView.add_message`, `src/pcli/tui/widgets/message_view.py`. Below that
threshold a message still prints as a plain, permanent line exactly as
before — a short notice like `Turn cancelled.` never grows a collapse
affordance. Most of pcli's short, "glance and maybe retype" command
confirmations (`/timeout`, `/models`, `/compact`'s guards, `/export`, etc.)
don't reach this codepath at all anymore — see [Slash
commands](#slash-commands) above — since they're shown as toast
notifications instead of transcript messages in the first place.

This addresses a real complaint: verbose output from `/help`, `/memory`, a
busy `/subagent`, or a long `!command` used to just pile up permanently in
the transcript with no way to reclaim the space. Unlike the tool-call/
tool-result/reasoning Collapsibles described above (which default to
*collapsed*), these default to **expanded** — you just asked to see this
output by running the command, so it's immediately visible; the only new
thing is the *option* to collapse it away afterward by clicking the title
bar (or focusing it and pressing Enter/Space, the same standard Textual
`Collapsible` interaction). The title follows the same `"{label} — {char
count} char(s)"` convention as a tool result's title; a shell message's
title additionally surfaces the command itself, e.g. `Shell: ls -la — 1,234
char(s)`, mirroring how a tool-result title shows the tool name
(`_message_title` in `message_view.py`). Styling is a muted left border for
`system` messages and an accent-colored one for `shell` messages
(`.message-collapsible-system`/`.message-collapsible-shell` in
`src/pcli/tui/styles/pcli.tcss`).

`user` and `assistant` messages (the primary conversation content) are never
wrapped, regardless of length. `decision` messages are excluded too,
deliberately — see [Decision log](#decision-log) below.

## Decision log

`record_decision` calls ([`tools.md`](tools.md#record_decision)) render
differently from other tool results. Instead of the generic
collapsed-by-default `Collapsible` described above,
`ChatScreen._show_decision_notice` (`src/pcli/tui/screens/chat.py`) shows
them as an always-visible message — `message_view.add_message("decision",
f"**{decision}**\n\n{rationale}")` — so a logged decision is immediately
scannable rather than tucked behind a click. In `_stream_response`'s
`tool_result` handling, this branch fires only for a successful (non-error)
`record_decision` call; an errored call still falls through to the normal
`add_tool_result` Collapsible. The rendering itself lives in a shared
`_render_decision_notice` helper that `_show_decision_notice` just calls
into, so [reloading/resuming a session](#reloadingresuming-a-session)
reconstructs the exact same notice inline, wherever the original
`record_decision` call still survives in `session.messages` — not a raw
tool-result Collapsible.

It's labeled `Decision` (`_ROLE_LABELS["decision"]` in
`src/pcli/tui/widgets/message_view.py`) and styled with a `$secondary` left
border (`.message-decision` in `src/pcli/tui/styles/pcli.tcss`) — distinct
from `.message-tool`'s `$warning` border and `.message-shell`'s `$accent`
border.

On `ChatScreen.on_mount`, *after* the transcript replay described in
[Reloading/resuming a session](#reloadingresuming-a-session) above, if the
session being opened already has recorded decisions (`Session.decisions`
non-empty — i.e. resuming a session that has some), a `system`-role summary
message is added on top: "Resuming with N recorded decision(s):" followed
by one `- <decision>` Markdown bullet per entry
(`decision` text only, not the `rationale`), built by `render_decisions()`
in `src/pcli/tools/builtin/decision_tool.py` — a real `"- "` bullet rather
than a plain `"• "` character, since this message renders through
`rich.markdown.Markdown` like any other `system` message (see [Collapsible
slash-command and shell output](#collapsible-slash-command-and-shell-output)
above) and needs actual list-item syntax to render as separate lines instead
of one run-on paragraph. This mirrors the existing resume-time summary shown
for `Session.todos` when it's non-empty, built the same way by
`render_todos()` in `src/pcli/tools/builtin/todo_tool.py`.

This aggregate summary is unconditional — it lists every entry in
`Session.decisions` regardless of whether that decision's originating
`record_decision` call still survives in `session.messages`. So a decision
made in a turn that's since been compacted away (see
[Auto-compaction](#auto-compaction) below) no longer appears inline via the
replay above, but still shows up here.

Also on `on_mount`, right after the `GatewayClient` is constructed, pcli adds
one more system message reporting the outcome of
[automatic context-limit detection](configuration.md#automatic-context-limit-detection)
for the active model — but only when there's something to report: it's
skipped entirely, with no message and no network calls, if pcli already has
a built-in or previously-set entry for that model. Otherwise you'll see
either "Auto-detected context limit for '<model>': N tokens." on success, or
"Couldn't auto-detect a context limit for '<model>' — pcli is assuming N
tokens. If that's wrong, set it with /context-limit <tokens>." if none of
the probes worked (the normal outcome for hosted gateways like OpenAI or
Anthropic).

## Status bar

One line normally, growing to two while a subagent is running
(`StatusBar._sync_height`, `src/pcli/tui/widgets/status_bar.py`):

```
[PLAN MODE]   ⠋ Working...   model: gpt-4o   cost: $0.0123   ctx: 3.2k/128.0k (2%)   tokens: 4.1k   tools: 2/25   sandbox: docker
⟳ Subagent: investigate failing test — 3 tool call(s), last: run_shell
```

Line 1:

- `[PLAN MODE]` tag — shown at the very start of the line, only while
  [plan mode](#plan-mode) is active (`StatusBar.plan_mode` reactive, set by
  `ChatScreen._set_plan_mode`). Absent entirely in normal ("build") mode.
- Spinner (braille frames, ticks at 10fps) — shown only while `busy` (set for
  the whole duration of a turn: waiting on the LLM, streaming, running tool
  calls — cleared once the response is fully printed).
- `model` — the active model id, shortened for display by
  `format_model_name()` (`src/pcli/tui/widgets/status_bar.py`) when it would
  otherwise be too long. This matters most for a raw `llama-server` instance
  (llama.cpp's own server, no LM Studio in front of it) with no `--alias`
  configured: it reports its "model" id as the full local path to the loaded
  GGUF file (e.g.
  `E:\.lmstudio\models\...\Mistral-Nemo-Instruct-2407-Q4_K_M.gguf`), unlike
  LM Studio's short ids (e.g. `qwen2.5-coder-7b-instruct`), and a raw path
  would flood the line and push `cost`/`ctx`/`tokens`/`sandbox` out of view.
  `format_model_name()` detects a path (contains `/` or `\`) and shows just
  the filename with a trailing `.gguf` stripped (the example above becomes
  `Mistral-Nemo-Instruct-2407-Q4_K_M`), rather than blindly truncating the
  path from the right, which would keep the least useful part (the
  drive/folder prefix) and cut off the identifying filename at the end.
  Anything still too long — a shortened path included — falls back to plain
  truncation via `truncate()`, capped at `_MODEL_NAME_MAX_LEN` (40 chars).
  Only this display string is shortened; the underlying model string used
  for actual API requests is untouched.
- `cost` — cumulative USD for the session (`Session.cost.session_total_usd`).
- `ctx` — tokens used / context-window limit and percentage, from the most
  recent LLM call's reported usage (only shown once a limit is known).
- `tokens` — cumulative total tokens for the session, formatted with
  k/m suffixes.
- `tools` — the main agent's own top-level tool-call count for the *current
  turn* (`StatusBar.main_tool_calls`), reset to 0 at the start of every turn
  (`ChatScreen._run_one_turn`) and incremented once per `tool_result` chunk
  the main loop itself produces. Deliberately separate from the subagent
  tool-call count on line 2 below: a subagent's own internal tool calls are
  never added to this count — only the main loop's own top-level calls are,
  so `spawn_subagent` itself counts here as exactly one call regardless of
  how much work the subagent does internally. Shown as `n/N` (e.g. `2/25`)
  against `StatusBar.main_tool_calls_limit`, re-read at the start of every
  turn from `guardrails.max_tool_calls_per_turn` — the same live-editable
  guardrail [`/max-tool-calls-per-turn`](#slash-commands) views/sets and
  `AgentLoop.run_turn` enforces as a hard per-turn cap — so you can see how
  much headroom is left before the guardrail would start denying tool calls
  this turn. Falls back to the plain `n` (no `/N`) when that guardrail is
  `0` (unlimited, its own existing convention, e.g. forced in
  [local-api mode](configuration.md#local-api-mode)), since there's no
  meaningful `N` to show a ratio against.
- `sandbox` — the backend actually selected at startup (`docker` or
  `subprocess`), so you always know the isolation level in effect.

Line 2 appears only while `spawn_subagent` is running (`subagent_task` is
non-`None`) and shows its task (truncated to 60 chars), tool-call count, and
the last tool it called; it disappears and the bar shrinks back to one line
once the subagent finishes. For the full tool-call history (name +
arguments, not just the last tool name) and any question the subagent is
currently blocked on, use [`/subagent`](#slash-commands) for a one-off
snapshot or [Ctrl+G](#ctrlg-subagent-activity-panel) for the same detail kept
live — this line is deliberately just a glanceable summary. Both read from
the same `ActivityTracker` instance that drives this line
(`ChatScreen._on_activity_changed`, subscribed via `ActivityTracker.subscribe`
in `on_mount`); the tracker supports multiple simultaneous subscribers, so
the status bar's own subscription and Ctrl+G's modal subscription don't
interfere with each other.

## Status pane (top)

`StatusPane` is now todos-only — a genuinely scrollable region (mouse
wheel/scrollbar), ~3 lines tall by default, with one child widget per todo
item in original list order (no reprioritizing or "+N more" truncation).
Each line reads `○/▶/✓ <content>` for pending/in-progress/completed. Whenever
the list changes (`watch_todos`), it rebuilds all the child widgets and then
auto-scrolls: if some todo is `in_progress`, that item is scrolled into view
(`scroll_visible`, deferred to after layout settles); otherwise it scrolls
back to the top (`scroll_home`).

The pane collapses to zero height (`display: none`) whenever the todo list is
empty. Subagent progress no longer lives here — see the status bar above.

A `write_todos` call that silently drops previously `completed` items (no
similar-enough counterpart in the new list) doesn't change how this pane
renders — it's a note appended to the tool's own result text, shown in the
normal collapsed tool-result `Collapsible` rather than here. See
[`tools.md#write_todos`](tools.md#write_todos).
