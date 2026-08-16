# TUI guide

Running `pcli` with no subcommand launches the Textual TUI (`PcliApp` ->
`ChatScreen`, `src/pcli/tui/`).

## Layout

Top to bottom (`ChatScreen.compose`):

- **Status pane** (`StatusPane`) — todo list only, ~3 lines, scrollable.
  Collapses to zero height when there's nothing to show.
- **Message view** (`MessageView`) — scrollable conversation history.
- **Status bar** (`StatusBar`) — cost/context/tokens/sandbox/spinner, plus a
  second line with subagent progress while one is running.
- **Input box** — where you type.

## Chat input

Type a message and press Enter to send it to the agent. It's appended to the
session, rendered in the message view, and streamed through `AgentLoop`,
which handles any tool calls the model makes (each may trigger a permission
prompt — see below) before the turn completes and the session is saved to
disk.

Anything starting with `/` is a slash command; anything starting with `!` is
shell passthrough, in one of three tiers (`!`, `!!`, `!!!` — all below). Plain
text otherwise goes to the model.

The input box is a `PasteInput` (`src/pcli/tui/widgets/paste_input.py`), a
thin `Input` subclass adding a **Shift+Insert** binding that pastes from the
real OS clipboard via the `pyperclip` package. This is distinct from
Textual's built-in Ctrl+V, which only reflects text copied *within* the app
(Textual's `App.clipboard` explicitly doesn't track the OS clipboard) and so
does nothing useful for text copied from outside pcli (a browser, another
terminal, an editor). Since the input box is single-line, only the first
line of multi-line clipboard content is inserted — the same truncation
Textual's own bracketed-paste handling already applies to `Input`. If
`pyperclip` can't reach a clipboard mechanism (e.g. a minimal Linux setup
without `xclip`/`xsel`/`wl-clipboard`), an error toast is shown instead of
crashing; an empty clipboard is a silent no-op. All of `Input`'s other
bindings (Ctrl+V, arrow keys, Ctrl+C copy, etc.) are unaffected — Textual
merges a subclass's `BINDINGS` with the parent's rather than replacing them.
The same `PasteInput` is also used for the import-path field in
`/sessions`'s import modal (`id="import-path-input"`,
`src/pcli/tui/screens/sessions.py`).

## Slash commands

- **`/sessions`** — opens the session list screen (`SessionListScreen`):
  browse previously stored sessions (title, model, cost, last-updated),
  `Enter` resumes one in a fresh `ChatScreen`, `e` exports the highlighted
  session, `i` prompts for a file path to import, `Esc` goes back.
- **`/export [path]`** — exports the *current* session via `export_session`
  to `path` if given, else `<data_dir>/exports/<session_id>.pcli-session.json`.
- **`/toolbox discover <name>`**, **`/toolbox list`**, **`/toolbox remove
  <name>`** — in-TUI equivalents of the `pcli toolbox ...` CLI subcommands;
  see [`toolbox-plugins.md`](toolbox-plugins.md). `discover`/`remove`
  additionally reload the tool registry so newly discovered tools are usable
  immediately.
- **`/models`** — with no argument, fetches the gateway's `GET /models` and
  opens a picker screen (`ModelListScreen`) to select interactively; with an
  argument (`/models <model-id>`), sets the model directly without a round
  trip. Either way the choice updates the running session, the status bar,
  and is persisted to `config.toml` (`update_config_file(default_model=...)`)
  so a bare `pcli` picks it up next time.
- **`/compact`** — manually summarizes and archives the oldest conversation
  history right now (see [`tools.md`](tools.md#auto-compaction)), bypassing
  the `auto_compact_threshold` check entirely. Still subject to
  `maybe_compact`'s own "not enough history yet" guard, reporting "Nothing to
  compact yet." if there isn't more history than
  `auto_compact_keep_recent_turns` turns.

Any other `/word` prints "Unknown command: /word".

## Auto-compaction

Compaction can also fire automatically and transparently, with no command
needed: at the end of every turn, once the session is saved, if
`auto_compact_enabled` is on (default) and context usage has reached
`auto_compact_threshold` (default 80% of the model's context limit),
`ChatScreen` runs the same summarization `/compact` triggers, reusing the
"Working..." spinner on the status bar while it does. When it fires, a short
system notice appears in the transcript (e.g. "Compacted N earlier
message(s)... archived as artifact_id='art_...'"); the actual summary text
that replaces the compacted messages is stored as a `role="system"` message
and, like the leading system prompt, is never rendered into the message
view. See [`configuration.md`](configuration.md#auto-compaction) for the
three settings involved.

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
- Typing `!!!` with nothing after it prints a usage message instead of doing
  anything.
- If the environment doesn't support suspending
  (`textual.app.SuspendNotSupported` — e.g. not supported in Textual Web), a
  plain fallback `system` message is shown instead of crashing: "Interactive
  shell handoff isn't supported in this terminal environment."

Like `!`/`!!`, `!!!` bypasses the LLM, the sandbox, permissions, and
session/artifact recording entirely. The input box's placeholder text
mentions it as `!!!interactive`.

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
- **Deny** — blocks this call; the model sees "Permission denied."

Grants are keyed by tool name only (not by specific arguments) — allowing
`run_shell` once for a given command allows it for any command afterward,
for the remainder of the granted scope. See
[`sandbox-and-permissions.md`](sandbox-and-permissions.md) for the full
decision flow (guardrails still apply and can override an "allow").

## Tool results

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
`add_tool_result` Collapsible.

It's labeled `Decision` (`_ROLE_LABELS["decision"]` in
`src/pcli/tui/widgets/message_view.py`) and styled with a `$secondary` left
border (`.message-decision` in `src/pcli/tui/styles/pcli.tcss`) — distinct
from `.message-tool`'s `$warning` border and `.message-shell`'s `$accent`
border.

On `ChatScreen.on_mount`, if the session being opened already has recorded
decisions (`Session.decisions` non-empty — i.e. resuming a session that has
some), a `system`-role summary message is added: "Resuming with N recorded
decision(s):" followed by one `• <decision>` line per entry (`decision` text
only, not the `rationale`), built by `render_decisions()` in
`src/pcli/tools/builtin/decision_tool.py`. This mirrors the existing
resume-time summary shown for `Session.todos` when it's non-empty.

## Status bar

One line normally, growing to two while a subagent is running
(`StatusBar._sync_height`, `src/pcli/tui/widgets/status_bar.py`):

```
⠋ Working...   model: gpt-4o   cost: $0.0123   ctx: 3.2k/128.0k (2%)   tokens: 4.1k   sandbox: docker
⟳ Subagent: investigate failing test — 3 tool call(s), last: run_shell
```

Line 1:

- Spinner (braille frames, ticks at 10fps) — shown only while `busy` (set for
  the whole duration of a turn: waiting on the LLM, streaming, running tool
  calls — cleared once the response is fully printed).
- `model` — the active model id.
- `cost` — cumulative USD for the session (`Session.cost.session_total_usd`).
- `ctx` — tokens used / context-window limit and percentage, from the most
  recent LLM call's reported usage (only shown once a limit is known).
- `tokens` — cumulative total tokens for the session, formatted with
  k/m suffixes.
- `sandbox` — the backend actually selected at startup (`docker` or
  `subprocess`), so you always know the isolation level in effect.

Line 2 appears only while `spawn_subagent` is running (`subagent_task` is
non-`None`) and shows its task (truncated to 60 chars), tool-call count, and
the last tool it called; it disappears and the bar shrinks back to one line
once the subagent finishes.

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
