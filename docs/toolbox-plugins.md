# Toolbox plugin system

The toolbox (`src/pcli/tools/toolbox/`) lets pcli discover OS/software
already installed on the machine (kubectl, Sun Grid Engine, Kafka, Apache
httpd, ...) and turn it into LLM-callable tools, without pcli having to ship
a hand-written integration for every possible piece of software.

Discovery is **only ever triggered explicitly** — by `pcli toolbox discover
<name>` or `/toolbox discover <name>` in the TUI. Nothing runs automatically
or in the background; a fresh pcli install has no toolbox tools until you ask
for one.

## Two discovery paths

`ToolboxManager.discover(software_name, ...)` (`toolbox/manager.py`) branches
on whether a **curated plugin** exists for that name:

### Curated plugins (`toolbox/plugins/`)

A `ToolboxPlugin` subclass declares candidate binary names, how to run/parse
a version check, and hand-written `CommandSpec`s (name, description, JSON-
schema parameters, an `build_args(arguments) -> argv` function, a resolved
`binary_path`, and a `risk` level of `read`/`mutate`/`destructive`). If found
on `PATH`, its tools are registered immediately — no LLM call needed. Curated
entries **re-detect live on every startup** (a `--version` probe is cheap and
catches upgrades), so nothing about them needs to be cached beyond a small
registry entry recording that they were discovered.

Existing curated plugins:

| Plugin | Binary/binaries | Notes |
|---|---|---|
| `kubectl` | `kubectl` | One binary, many subcommands: `get`, `describe`, `logs`, `get_contexts` (read); `apply`, `scale` (mutate); `delete` (destructive). |
| `sge` (Sun/Univa Grid Engine) | `qstat` (primary) + `qhost`, `qacct`, `qsub`, `qdel` if present | **Multi-binary suite** — each sibling binary is resolved independently via `shutil.which`, and its tools are only registered if that specific binary is found. `qstat`/`qhost`/`qacct` read; `qsub` mutate; `qdel` destructive. |
| `kafka` | `kafka-topics(.sh/.bat)` (primary) + `kafka-consumer-groups(.sh/.bat)` if present | **Multi-binary suite**, same independent-resolution pattern. Topic list/describe/create/delete + consumer-group list/describe. |
| `httpd` | `apachectl`, `apache2ctl`, `httpd`, or `apache2` (first found) | `configtest`, `dump_vhosts`, `dump_modules` (read); `graceful_restart`, `restart` (mutate); `stop` (destructive). |

### LLM-synthesized tools (uncurated software)

If no curated plugin matches the name, `ToolboxManager` falls back to
generic introspection:

1. `collect_help_corpus(binary_path, cwd)` (`toolbox/introspect.py`) runs
   `<binary> --help`, regex-guesses up to 8 subcommand names from indented
   lines in the output (`_SUBCOMMAND_RE`), and runs `<binary> <subcommand>
   --help` for each — a best-effort text corpus, not a real argparse/man-page
   parser.
2. The corpus is hashed (`store.hash_corpus`); if a cached synthesized schema
   exists for this exact hash, it's reused (no LLM call).
3. Otherwise `synthesize_tools(...)` (`toolbox/synthesize.py`) sends the
   corpus (capped at 12,000 chars) to the configured gateway with a system
   prompt instructing it to emit a JSON `{"tools": [...]}` list — each tool
   an argv `subcommand`, a JSON-schema `parameters` object for flags, and a
   `risk` level. The response is `jsonschema`-validated; on failure it
   retries once with the validation error fed back, then gives up rather than
   registering something malformed.
4. Valid results are cached at `toolbox_dir()/<name>/synthesized.json`
   (keyed by the corpus hash, so an unchanged install skips resynthesis on
   future runs) and registered.

Synthesized tools use a generic flag-building convention
(`_auto_flags`): each provided argument becomes `--arg-name value` (snake_case
converted to kebab-case), booleans become bare flags, lists repeat the flag.
Their description is suffixed with "(auto-generated from --help output;
unreviewed, verify before trusting blindly)" — unlike curated plugins, these
haven't been hand-reviewed against real CLI syntax (positional args, short
flags, `=`-joined values don't fit this convention).

Both curated and synthesized command tools run through `ctx.sandbox` (same
as `run_shell`) and require permission unless their `risk` is `"read"`.

## Self-authored scripts

`ToolboxManager.discover(software_name, ..., path=...)`
(`src/pcli/tools/toolbox/manager.py:160`) has an optional `path` parameter:
instead of requiring the software to already be on `PATH`, discovery can
point directly at a script — including one just written with `write_file`,
e.g. a small Python helper for something expected to be needed repeatedly.
When `path` is given, curated-plugin matching is skipped entirely (pointing
at a specific script means "use exactly this," not "look something up") and
discovery goes straight through the same LLM-synthesized path described
above: `--help` corpus collection, hashing/caching, synthesis.

- **`.py` scripts are invoked via `sys.executable`**
  (`_invocation_for_path`, `src/pcli/tools/toolbox/manager.py:100`) rather
  than executed directly — this works cross-platform, including Windows,
  without needing a shebang line or the execute bit a self-authored script
  can't be relied on to have.
- **The path must resolve inside the current working directory**
  (`_resolve_script_path`, `src/pcli/tools/toolbox/manager.py:141`) — the
  same containment rule `RestrictedSubprocessSandbox._validate_cwd` enforces
  for everything else in the sandbox (see
  [`sandbox-and-permissions.md`](sandbox-and-permissions.md#restrictedsubprocesssandbox)).
  A path that resolves outside it is rejected, not silently redirected.

Two ways to trigger it:

- **Human-triggered:** `pcli toolbox discover <name> --path <path>` on the
  CLI (`src/pcli/cli.py:135`), or `/toolbox discover <name> [path]` in the
  TUI — an optional second word after the name
  (`ChatScreen._handle_toolbox_command`, `src/pcli/tui/screens/chat.py:450`).
  Both were human/slash-command-only before this parameter existed.
- **Model-triggered:** the `register_toolbox_tool` tool
  (`src/pcli/tools/builtin/toolbox_register_tool.py`) lets the model call
  discovery itself, mid-conversation, instead of that being human-only — see
  [`tools.md`](tools.md#register_toolbox_tool). This is the intended path for
  a model that just authored its own reusable script and wants a real tool
  wrapping it, rather than re-deriving the same shell command every time it's
  needed. Unlike the human-triggered paths above, `register_toolbox_tool` is
  **always** permission-gated (`needs_permission=True` unconditionally) since
  it grants standing execution rights rather than running a single command.

## Persistence

- `toolbox_dir()/registry.json` — `{software_name: {source, binary_path,
  version, tool_count}}`, the top-level "what's been discovered" list shown
  by `list`.
- `toolbox_dir()/<name>/synthesized.json` — cached synthesized schema for
  uncurated software (curated plugins don't need this; they re-detect live).

On every `ChatScreen` startup, `ToolboxManager.load_all()` rebuilds tool
specs for everything in the registry: curated entries re-probe their
binaries live (skipped silently, not an error, if the binary's gone missing
since discovery — a stale registry entry shouldn't break startup); synthesized
entries load their cached schema and re-resolve the binary path via
`shutil.which`.

## Commands

CLI (`pcli toolbox ...`, `src/pcli/cli.py`):

- `pcli toolbox discover <name>` — runs discovery (as above); prints a
  one-line summary. Uses the configured gateway if one is set (needed only
  for the synthesis fallback), and works without one for curated plugins.
- `pcli toolbox list` — prints each discovered entry: name, `[source]`
  (`curated`/`synthesized`), version, tool count.
- `pcli toolbox remove <name>` — drops an entry from the registry (exits 1 if
  not present).

TUI equivalents (`/toolbox discover|list|remove ...`, see
[`tui-guide.md`](tui-guide.md)) behave the same, plus `discover`/`remove`
reload the live `ToolRegistry` so new/removed tools take effect immediately
without restarting.
