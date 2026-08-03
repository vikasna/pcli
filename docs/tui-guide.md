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
shell passthrough (both below). Plain text otherwise goes to the model.

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

## Shell passthrough: `!command` and `!!command`

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
