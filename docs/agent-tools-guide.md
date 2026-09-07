# How to create and register an agent tool

An **agent tool** is a named, reusable subagent persona: a fixed system
prompt (the persona) plus a fixed, restricted list of already-existing tool
names, wrapped up as an ordinary single-argument (`query`) tool the model can
call from then on. Under the hood it's the exact same mechanism as
`spawn_subagent` — a fresh, independent `AgentLoop` sharing the parent's
gateway/permissions (`make_agent_tool`, `src/pcli/tools/agent_tools.py`) — the
only difference is *when* the persona and allowed-tools get decided: with
`spawn_subagent` the calling model spells them out fresh on every call; with
an agent tool they're baked in once, at registration time, and reused.

This guide walks through actually creating one, end to end. For the full
parameter/behavior reference, see
[`tools.md#register_agent_tool`](tools.md#register_agent_tool) — this guide
won't repeat that, only link to it.

## When to reach for one vs. `spawn_subagent`

- **`spawn_subagent`** — ad hoc, per-call. Use it for a one-off delegation:
  "go check how X is implemented and report back." Every call restates the
  task, and optionally an `allowed_tools` list, from scratch.
- **An agent tool** — named, persisted, reusable. Worth defining once the
  model (or you) notices the *same kind* of focused sub-task keeps coming up
  with the same persona and the same safe toolset — e.g. "explore the
  codebase read-only," "dig through log files," "investigate test failures."
  Once registered, calling it is just `some_tool_name(query="...")` instead
  of re-deriving the task description, persona, and allowed-tools list every
  time.

pcli ships seven such tools by default. `explore_codebase`, `explore_files`,
`explore_logs` are the simplest, most uniformly read-only examples of the
pattern (see
[`tools.md#explore_codebase-explore_files-explore_logs`](tools.md#explore_codebase-explore_files-explore_logs))
and the best template to imitate. `write_documentation`, `verify_computation`,
`deep_research`, `data_analysis` are four more built-in ones for other common
delegation patterns — including ones whose `allowed_tools` aren't uniformly
read-only, so `plan_mode_safe` is `false` for three of the four (see
[`tools.md#write_documentation-verify_computation-deep_research-data_analysis`](tools.md#write_documentation-verify_computation-deep_research-data_analysis)
for the full reference and the plan-mode-safety reasoning per tool).

## Two ways to create one

### 1. Ask the model to register one mid-conversation (the primary path)

`register_agent_tool` is itself a tool — the intended way to create an agent
tool is for the model to call it directly during a normal conversation, not
for a human to hand-edit a config file. In practice this means just asking
for it in plain language.

**Example prompt:**

> "You keep spawning subagents to investigate test failures the same way
> each time — read the failing test file, grep for related fixtures, read
> the test runner's log output. Register that as a reusable agent tool
> called `investigate_test_failure` instead of re-explaining it every time."

The model would then call `register_agent_tool` with something like:

```json
{
  "name": "investigate_test_failure",
  "description": "Delegate investigation of a failing test (e.g. 'why does test_foo fail', 'what changed that broke test_bar') to a subagent restricted to read-only test-related tools.",
  "persona_prompt": "You are a test-failure investigation subagent spawned by another AI agent (pcli). Given a description of a failing test, use the tools available to you to find the test's source, related fixtures/config, and any relevant log output, then give a clear, self-contained explanation of why it's failing — reference file_path:line where it helps. The parent agent only sees your final text, not your intermediate steps.",
  "allowed_tools": ["read_file", "list_dir", "glob_search", "grep"],
  "plan_mode_safe": true
}
```

This is a plausible new agent tool distinct from the three built-in ones:
it's scoped to read-only filesystem/search tools (no `run_shell`, even though
"run the test and see" is tempting — see the scoping guidance below), so
`plan_mode_safe: true` is a legitimate choice here.

Once the call succeeds, the tool is live immediately — see
[Persistence and lifecycle](#persistence-and-lifecycle) below — and callable
in that same session as `investigate_test_failure(query="...")`.

### 2. Add a new built-in default (for pcli contributors)

If an agent tool should exist for *every* pcli session rather than being
registered ad hoc, add it as a built-in default instead of relying on
`register_agent_tool` at runtime:

1. In `src/pcli/tools/agent_tools.py`, follow the shape of
   `EXPLORE_CODEBASE`/`EXPLORE_FILES`/`EXPLORE_LOGS`: define a persona-prompt
   string, then call `make_agent_tool(name=..., description=...,
   persona_prompt=..., allowed_tool_names=[...], plan_mode_safe=...)` and
   assign the result to a module-level constant.
2. In `src/pcli/tools/registry.py`'s `build_default_registry()`, import the
   new constant and add it to the tuple of tools passed to
   `registry.register(...)` — right next to `EXPLORE_CODEBASE`, `EXPLORE_FILES`,
   `EXPLORE_LOGS`, `REGISTER_AGENT_TOOL`.

That's the whole mechanism — `make_agent_tool` doesn't care whether it's
called from a hardcoded module constant or from the `register_agent_tool`
handler at runtime; both paths produce the same kind of `ToolSpec`.

## Scoping `allowed_tools` well

The canonical illustration of good scoping is `explore_logs`: it's restricted
to `read_file` and `grep` only. `run_shell` would be a natural fit for
something like `tail`ing a log file, but it's deliberately excluded so the
tool stays *uniformly* read-only for its stated purpose — no mutating or
sandboxed-execution capability sneaks in "just in case it's useful."

Apply the same discipline when defining your own: list only the tools the
persona actually needs to do its one job, and don't add a tool because it
*might* come in handy. A narrower `allowed_tools` list is also what makes
`plan_mode_safe: true` legitimate — see below.

## `plan_mode_safe`

`plan_mode_safe` controls whether the new agent tool stays callable while
[plan mode](tui-guide.md#plan-mode) is active. Set it `true` **only if every
tool named in `allowed_tools` is itself read-only/exploration-only** — if
even one allowed tool can write, edit, or execute something, leave it at the
default `false`.

This isn't just a labeling nicety: if the parent turn is in plan mode, the
nested subagent's own effective tool set is filtered down to `plan_mode_safe`
tools only regardless of what `allowed_tools` says (`make_agent_tool`'s
`_allowed` check) — so an agent tool can't be used to route a mutating call
around plan mode's restrictions. See
[`tui-guide.md`'s Plan mode section](tui-guide.md#plan-mode) for the full
three-layer enforcement story; this guide won't repeat it.

All three built-in `explore_*` tools set `plan_mode_safe=True` because their
entire allowed-tool lists are read-only.

## Persistence and lifecycle

- A successful `register_agent_tool` call does two things at once: it saves
  the definition to `agent_tools.json` in pcli's data directory
  (`save_agent_tool`, `src/pcli/tools/agent_tools_store.py`), and it
  registers the tool live into the current session's `ToolRegistry`.
- Because it's registered live immediately, **it's callable in the same
  session, right after registration — no restart needed.**
- Because it's also saved to disk, **it survives restarts.** On the next
  startup, `ChatScreen.on_mount` calls `load_persisted_agent_tools()`
  (`src/pcli/tools/agent_tools_store.py`), which rebuilds a `ToolSpec` for
  every saved definition and merges them into the registry, announcing
  "Loaded N previously-registered agent tool(s)." if any were found.

## Permission

Registering an agent tool is **always permission-gated**, unconditionally —
regardless of `plan_mode_safe` or how narrow `allowed_tools` is. There's no
risk-based exception the way some other tools get one for a `"read"` risk
level: `register_agent_tool` has `needs_permission=True` with no exceptions,
because it grants standing execution rights to a nested subagent, which is a
bigger action than running one command.

## Common mistakes / gotchas

- **Referencing a tool name that doesn't exist.** `register_agent_tool`
  validates every entry in `allowed_tools` against the caller's own tool
  registry before saving anything. Any unknown name fails the whole call
  cleanly with `Unknown tool name(s): ...` plus a suggestion to only
  reference tools that actually exist — nothing gets persisted or registered
  if validation fails.
- **Expecting the persona to remember the parent conversation.** Just like
  `spawn_subagent`, the nested subagent only gets the `persona_prompt` as its
  system message and the `query` as its first user message — it starts with
  no memory of anything said earlier in the parent session. Write
  `persona_prompt` and the `query` you pass at call time to be
  self-contained.
- **Expecting it to spawn further subagents or register more agent tools.**
  A nested agent tool's tool set is filtered to exactly `allowed_tools`
  (further intersected with `plan_mode_safe` tools if the parent is in plan
  mode) — `spawn_subagent` and `register_agent_tool` are only reachable at
  all if you explicitly list them in `allowed_tools` (neither is in any of
  the three built-in `explore_*` tools' lists). `spawn_subagent` additionally
  hard-excludes itself by name from whatever sub-registry it builds, every
  time it runs (`_spawn_subagent`'s `_allowed` check,
  `src/pcli/tools/builtin/subagent_tool.py`) — so even where it is reachable,
  delegation through it is structurally capped at one further level, not
  just discouraged by convention.
