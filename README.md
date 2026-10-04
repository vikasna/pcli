<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img src="assets/logo.svg" alt="pcli logo" width="72" height="72">
</picture>

# pcli

**An opencode-style AI coding agent, as a Python TUI, that talks to any OpenAI-compatible gateway.**

pcli is a terminal coding agent: a tool-calling agent loop with real filesystem/shell/browser access, running against your working directory, rendered in a Textual TUI. It connects to any OpenAI-compatible chat-completions endpoint — a hosted API, an aggregator, or a model running entirely on your own machine — rather than being locked to one vendor. Beyond the usual session management, cost tracking, and permission/guardrail system you'd expect from a coding agent, it adds a few things built specifically because no other agent CLI does them well: a hard, enforced per-session cost budget; a scheduler that can react to file changes and git commits, not just cron time; and an opt-in tamper-evident audit log for unattended runs in regulated environments.

See [`docs/README.md`](docs/README.md) for the full architecture reference and every per-topic doc.

## What sets pcli apart

### Hard session cost-budget enforcement

Most agent CLIs only *report* what a session cost after the fact. pcli can actually stop itself once a configured budget is hit — it doesn't just log an overrun, it refuses to keep spending.

The check (`cost_budget_reason`) runs at the start of **every internal LLM round-trip** inside a turn, not once per user-visible turn — so a single tool-call-heavy turn can't blow through the cap across many internal iterations before control ever returns to you. It's wired into all four places that drive an agent loop: the main TUI session, headless/scheduled runs, subagents, and memory extraction, since subagent and compaction spend fold into the same session total.

- `/budget [amount|off]` — view or set the cap live in the TUI
- `PCLI_MAX_SESSION_COST_USD` / `max_session_cost_usd` in `config.toml` — the persisted cap (unset by default: no limit)
- `pcli run --max-cost <amount>` / `pcli schedule add --max-cost <amount>` — a per-invocation override that never touches the persisted setting

### Event-triggered scheduling

`pcli schedule` is pcli's own recurring-task daemon, and it isn't limited to cron timing. Besides `--cron "*/15 * * * *"`, a job can be told to fire on an *event*:

- `--on-file-change <path>` — fires when a watched file or directory (hashed recursively by path/mtime/size, no contents read) changes
- `--on-git-commit [--git-repo PATH] [--git-branch NAME]` — fires when a new commit lands on a watched branch

Both still run through the same poll loop as a cron job (`pcli schedule run --poll-interval`, default 30s) — there's no OS-level file-watching integration or git hook, just a cheap check on every tick, so no new dependency was needed. The detail worth knowing before you rely on it: **self-trigger-loop prevention**. After an event job fires, the daemon doesn't reuse the signature/SHA that triggered the run as its new baseline — it re-checks *after* the job's own run finishes and stores that as the baseline instead. So a task that edits its own watched file, or commits to its own watched branch (a very plausible "auto-commit the output" pattern), does not see its own output as one more change and fire again in a loop.

### Tamper-evident, hash-chained audit log

Opt-in (`audit_mode_enabled`, off by default — real per-tool-call overhead most users don't need). When it's on, pcli keeps a second, independent record of what a run actually did: every tool call and every permission/guardrail decision is appended as an `AuditEntry` to `Session.audit_log`, each entry's hash computed over its own fields plus the previous entry's hash — the same tamper-evidence primitive a git commit chain uses. Edit, reorder, or delete an old entry and the chain breaks from that point forward; the break can't be silently repaired without re-deriving every hash after it.

`pcli sessions verify <id>` recomputes the chain and tells you whether it's intact or exactly where it broke:

```
$ pcli sessions verify sess_a1b2c3d4
12 audit entries, chain valid.

$ pcli sessions verify sess_tampered
Chain broken at entry 3: entry_hash does not match the recomputed hash - this entry's content was modified after it was recorded.
```

This is aimed at regulated-industry use — finance, healthcare, government — where someone needs to prove, after the fact, what an unattended agent did, and that the record wasn't quietly edited afterward. Turn it on for a single invocation without touching the persisted setting via `pcli run --audit` or `pcli schedule add --audit`.

## Also notable

- **`pcli tools list`** — a static CLI listing of all 43 built-in tools, each with an audited read-only-vs-mutating classification, `needs_permission`, `plan_mode_safe`, and a one-line description. No gateway, session, or config needed to run it.
- **Toolbox plugins** — `pcli toolbox discover <name>` turns an installed OS/software CLI (kubectl, Sun Grid Engine, Kafka, Apache httpd, or anything with a `--help`) into LLM-callable tools automatically — curated plugins for the common ones, LLM-synthesized tool schemas (cached, validated) for anything else.
- **`--local-api` mode** — uncaps tool-iteration and rate-limit guardrails and forces cost to `$0` for a given local gateway, paired to that specific gateway URL rather than a global switch. Local models get first-class treatment, not an afterthought.
- **Global cross-session memory** — a small, durable profile of what pcli has learned about you (nature of work, preferences, recurring task patterns), injected into every new session's system prompt, extended automatically on compaction.
- **Real browser automation** — seven `browser_*` tools drive an actual Chromium via Playwright (click, type, wait, read, screenshot), watchable live and headed in the TUI, or headless under `pcli run`/`pcli schedule`.
- **Telegram bot front end** — `pcli telegram` runs pcli as a two-way bot with inline-button permission approval, so you can approve or deny a tool call from your phone.

## Install

If you just want to run pcli, install it from PyPI:

```
pip install pcli-agent
```

The distribution is named `pcli-agent` on PyPI (`pcli` was already taken by an unrelated package) — the command you run afterward is still `pcli`. Want one of the optional features below at install time? Add extras the same way (quote the argument — most shells treat `[...]` specially):

```
pip install "pcli-agent[browser,telegram]"
```

### Working on pcli itself

Contributing, or want an editable install from a clone of this repo? Install from `pyproject.toml` directly, with the `dev` extra for the test/lint/type-check tooling:

```
pip install -e ".[dev,docker,win,browser,telegram,schedule]"
```

Either way, pick the extras you actually need — none of them are required for the core TUI/CLI to run:

| Extra | What it's for |
|---|---|
| `dev` | pytest, ruff, mypy — only needed for working on pcli itself. |
| `docker` | Intentionally empty — the Docker sandbox backend shells out to the `docker` CLI directly, no Python SDK needed. Just makes `pip install -e ".[docker]"` valid; requires `docker` on `PATH` at runtime. |
| `win` | `pywin32`, for Windows-specific functionality. |
| `browser` | `playwright`, for the `browser_*` tools (see below — needs a one-time extra step). |
| `telegram` | `python-telegram-bot`, for `pcli telegram`. Self-sufficient — no separate download. |
| `schedule` | `croniter`, for `pcli schedule`'s cron expression parsing. Self-sufficient — no separate download. |

If you installed the `browser` extra, Chromium itself is a separate ~300MB download, fetched once:

```
playwright install chromium
```

## Quickstart

The fastest way to see pcli working needs no API key or signup at all: point it at a local model server.

With [LM Studio](https://lmstudio.ai/) (load a model, start its local server from the Developer tab — it defaults to `http://localhost:1234/v1`):

```
pcli --gateway-url http://localhost:1234/v1 --model <model-id-from-lm-studio>
```

With [Ollama](https://ollama.com/) (serves an OpenAI-compatible endpoint under `/v1`):

```
pcli --gateway-url http://localhost:11434/v1 --model llama3
```

Either way, the gateway URL and model are saved to `config.toml` on first use, so a bare `pcli` afterward reconnects without repeating the flags. Not sure of the exact model id your server expects? Launch pcli and run `/models` — it lists whatever the server reports and lets you pick one interactively.

Add `--local-api` to lift the default tool-iteration/rate-limit guardrails (sized for paid, rate-limited gateways) and force cost tracking to `$0` for that gateway:

```
pcli --gateway-url http://localhost:1234/v1 --model <model-id> --local-api
```

## Connecting to a provider

**pcli's gateway client speaks the OpenAI chat-completions wire format only** — `POST {base_url}/chat/completions`, `GET {base_url}/models`. There is no native Anthropic Messages API client and no native Google Gemini API client in this codebase. This genuinely covers a lot of ground, but it's worth being precise about what it does and doesn't mean:

- **Direct, native support**: OpenAI itself, plus any server or provider that exposes an OpenAI-compatible `/v1/chat/completions` endpoint — which includes most of the ecosystem. Local servers: LM Studio, Ollama, vLLM, text-generation-webui, llama.cpp server. Hosted APIs: Groq, Mistral, DeepSeek, Together AI, Fireworks AI, and OpenRouter.
- **Not natively supported**: Anthropic's own Messages API and Google's own Gemini API use different request/response shapes than OpenAI's chat-completions format, so pcli cannot talk to `api.anthropic.com` or Google's native Gemini endpoint directly. The honest way to reach a Claude or Gemini model *through* pcli is via an OpenAI-compatible proxy or aggregator — **OpenRouter** is the simplest one, and it fronts both model families behind one OpenAI-compatible endpoint.
- **Azure OpenAI is not currently supported.** Its deployment-based URL carries a mandatory `api-version` query parameter, and pcli's gateway client always appends `/chat/completions` as a literal path segment onto `gateway_base_url` — the two don't compose into a URL Azure will accept. See the "Azure OpenAI" section below for details.

### OpenAI

```
# PowerShell
$env:PCLI_GATEWAY_URL = "https://api.openai.com/v1"
$env:PCLI_GATEWAY_API_KEY = "sk-..."
pcli --model gpt-4o
```

### LM Studio / Ollama (local)

See [Quickstart](#quickstart) above — no API key needed.

### OpenRouter (the path to Claude, Gemini, and others)

```
$env:PCLI_GATEWAY_URL = "https://openrouter.ai/api/v1"
$env:PCLI_GATEWAY_API_KEY = "sk-or-..."
pcli --model anthropic/claude-sonnet-4.5
```

OpenRouter exposes one OpenAI-compatible endpoint in front of dozens of providers' models, including Anthropic's and Google's — swap `--model` for `google/gemini-2.5-pro` or any other model it hosts, and the same gateway URL/key keep working.

### Also just works, same pattern

Groq, Mistral, DeepSeek, Together AI, and Fireworks AI all expose an OpenAI-compatible `/chat/completions` endpoint — point `gateway_base_url`/`PCLI_GATEWAY_URL` at theirs, set the matching API key, and pick one of their model ids. No special-casing needed.

### Azure OpenAI

**Not supported.** `GatewayClient` builds an `httpx.AsyncClient` from `gateway_base_url` and then issues every request against a relative path — `POST /chat/completions`, `GET /models` — which httpx appends as a literal path segment onto whatever `gateway_base_url` is. Azure's deployment URL already ends in a mandatory `api-version` query string (e.g. `.../deployments/<deployment>?api-version=2024-02-01`), and httpx's URL-joining doesn't relocate that query string when a path is appended — it just concatenates, producing `.../deployments/<deployment>?api-version=2024-02-01/chat/completions`. That's not a valid request: `/chat/completions` ends up folded into the `api-version` value instead of becoming a real path segment, so there's no URL shape you can put in `gateway_base_url` that works. Reaching Azure-hosted models through pcli would need dedicated client-side support (a special case for the Azure URL shape); until then, use OpenAI directly or an aggregator such as OpenRouter.

## Configuration

Settings come from environment variables, CLI flags, or `config.toml`. Precedence, highest to lowest: **CLI flags > environment variables > `config.toml` > built-in defaults**. `config.toml` lives at `<config_dir>/config.toml` (e.g. `%APPDATA%\pcli\config.toml` on Windows, `~/.config/pcli/config.toml` on Linux) — created on first access if missing.

| Setting | Env var | CLI flag | config.toml key | Default |
|---|---|---|---|---|
| Gateway base URL | `PCLI_GATEWAY_URL` | `--gateway-url` | `gateway_base_url` | `""` |
| Gateway API key | `PCLI_GATEWAY_API_KEY` | `--api-key` | `gateway_api_key` | `""` |
| Default model | `PCLI_MODEL` | `--model` | `default_model` | `""` |
| Artifact threshold (chars) | `PCLI_ARTIFACT_THRESHOLD_CHARS` | `--artifact-threshold` | `artifact_threshold_chars` | `4000` |
| Local-API gateways | `PCLI_LOCAL_API_GATEWAYS` | `--local-api` | `local_api_gateways` | `[]` |
| Hard session cost cap (USD) | `PCLI_MAX_SESSION_COST_USD` | *(none — see `pcli run --max-cost`)* | `max_session_cost_usd` | unset (no cap) |
| Tamper-evident audit log | `PCLI_AUDIT_MODE_ENABLED` | *(none — see `pcli run --audit`)* | `audit_mode_enabled` | `false` |
| Sandbox backend | `PCLI_SANDBOX_BACKEND` | *(none)* | `sandbox_backend` | `"auto"` (`auto`\|`docker`\|`subprocess`\|`none`) |
| Request timeout (s) | `PCLI_REQUEST_TIMEOUT_S` | *(none)* | `request_timeout_s` | `120.0` |
| Max tool-call iterations/turn | `PCLI_MAX_TOOL_ITERATIONS` | *(none)* | `max_tool_iterations` | `25` |

The API key is deliberately never auto-persisted to `config.toml` — pass `--api-key` or set `PCLI_GATEWAY_API_KEY` each time if your gateway needs one. The gateway URL, model, and artifact threshold *are* auto-persisted when passed as a flag, so a bare `pcli` afterward picks them up. See [`docs/configuration.md`](docs/configuration.md) for the complete settings table — this covers only the ones you're most likely to touch.

Example `config.toml`:

```toml
gateway_base_url = "http://localhost:1234/v1"
default_model = "qwen2.5-coder-32b-instruct"
max_session_cost_usd = 5.0
sandbox_backend = "subprocess"
audit_mode_enabled = false
```

## FAQ

**Does my code or data ever leave my machine?**
Only to whatever gateway you've configured. Point `gateway_base_url` at a local server (LM Studio, Ollama, vLLM, ...) and nothing leaves your machine at all — pcli's own cost/pricing tables and sandbox execution are entirely local regardless of gateway. Point it at a hosted API and your conversation (including file contents the model reads) goes wherever that API sends it, same as any coding agent.

**How is this different from other AI coding agent CLIs?**
Three things specifically: a cost budget that's actually *enforced* mid-turn rather than just reported after the fact, a scheduler that can react to file changes or git commits instead of cron time alone, and an opt-in hash-chained audit log for proving what an unattended run did. See [What sets pcli apart](#what-sets-pcli-apart) above. It's also provider-agnostic by construction — any OpenAI-compatible gateway, local or hosted — rather than built around one vendor's API.

**Can I run this unattended, in CI, or on a schedule?**
Yes — `pcli run --task "..."` is a one-shot, non-interactive invocation with a proper exit code (0 on success, 1 on a hit iteration/cost cap or a usage error), suitable for cron/Task Scheduler. `pcli schedule` is pcli's own recurring-task daemon on top of the same machinery, with cron or event triggers. Both support `--max-cost` and `--audit` as safety nets for an unattended run, since there's no one present to approve a permission prompt (a tool call with no standing "Always Allow" grant is simply denied, not paused to ask).

**What stops the model from running something destructive?**
Two independent layers. Guardrails (`guardrails.toml`) are hard, non-negotiable denials — a shell command denylist, filesystem allow/deny roots, a Python module denylist — checked before any permission prompt and never bypassable by asking. On top of that, the permission system gates every tool call that isn't explicitly read-only, either against a remembered grant or an interactive prompt (fails closed with no UI, e.g. in a headless run). Underneath both, a sandbox backend (Docker, a restricted subprocess, or — only if you explicitly opt out — none) bounds what an allowed command can actually touch. See [`docs/sandbox-and-permissions.md`](docs/sandbox-and-permissions.md).

**Does it work fully offline?**
Yes, with a local gateway (LM Studio, Ollama, vLLM, ...) — no network call leaves your machine except whatever the model server itself does. `web_search`/`web_fetch`/`browser_*` are the only tools that reach the internet, and the model only calls them if it decides to.

**Does it work on Windows?**
Yes. pcli's system prompt tells the model up front that it's running on Windows and that `run_shell` executes via `cmd.exe`, not bash — no heredoc syntax, no `$VAR` expansion, chain with `&&` not `;` — rather than letting the model default to Unix assumptions and discover it's wrong by trial and error. The `win` extra (`pywin32`) covers Windows-specific functionality beyond that.

**What does it cost to run?**
Entirely up to the gateway and model you point it at — pcli has no pricing of its own, it tracks whatever a model actually costs against a best-effort pricing table you can edit. Point it at a local model with `--local-api` and it's `$0`, enforced (cost is literally zeroed, not just expected to be low, since a locally-served model's name can coincidentally match a paid builtin pricing pattern otherwise).

**Is this an editor plugin? Does it replace my IDE?**
No. pcli is a standalone terminal TUI you run alongside your editor, not a plugin inside one — it complements an IDE rather than replacing it.

## Learn more

[`docs/README.md`](docs/README.md) is the full reference: architecture, every setting, every built-in tool, the TUI guide, sandboxing/permissions, sessions/cost, memory, headless/scheduled runs, browser automation, the Telegram bot, and the toolbox plugin system.

## License

MIT — see [`LICENSE`](LICENSE).
