# pcli

An opencode-style AI coding agent CLI/TUI for Python. Connects to any OpenAI-compatible LLM gateway, with session management, cost tracking, permission handling, guardrails, tiered sandboxed execution, a lazy-loading Python discovery tool, a global cross-session memory of the user, a toolbox for discovering and using installed OS software (kubectl, SGE, Kafka, httpd, ...), one-shot non-interactive task execution (`pcli run`) for cron/Task Scheduler-driven runs, its own crontab-like recurring task scheduler (`pcli schedule`) as an alternative to an OS-level scheduler, real browser automation (click/type/navigate a live Chromium via Playwright, watchable in the TUI or headless under `pcli run`), and a two-way Telegram bot front end (`pcli telegram`) with inline-button permission approval.

See `requirements.md` for the original project brief. For a full reference
(architecture, every setting, every tool, the TUI, sandboxing/permissions,
sessions/cost, and the toolbox plugin system), see [`docs/README.md`](docs/README.md).

## Development

```
pip install -e ".[dev,docker,win,browser,telegram,schedule]"
pcli
```

The `browser` extra only pulls in the `playwright` Python package; the
browser binary itself is a separate one-time download — run `playwright
install chromium` once after installing. See
[`docs/browser-automation.md`](docs/browser-automation.md). The `telegram`
extra, unlike `browser`, needs no separate download — installing it is the
whole setup for `pcli telegram`. See
[`docs/telegram-bot.md`](docs/telegram-bot.md). The `schedule` extra
(`croniter`) is likewise self-sufficient, and is only needed for `pcli
schedule` — pcli's own crontab-like recurring task scheduler. See
[`docs/scheduling.md`](docs/scheduling.md).

## Configuring the gateway

pcli talks to any OpenAI-compatible chat-completions endpoint. Configure it via
environment variables, CLI flags, or `config.toml` (see `pcli.config.paths.config_file()`
for its location) — precedence is CLI flags > env vars > `config.toml` > defaults.

| Setting | Env var | CLI flag | config.toml key |
|---|---|---|---|
| Base URL | `PCLI_GATEWAY_URL` | `--gateway-url` | `gateway_base_url` |
| API key (optional) | `PCLI_GATEWAY_API_KEY` | `--api-key` | `gateway_api_key` |
| Default model | `PCLI_MODEL` | `--model` | `default_model` |
| Artifact threshold (chars) | `PCLI_ARTIFACT_THRESHOLD_CHARS` | `--artifact-threshold` | `artifact_threshold_chars` |

The API key is optional — local, unauthenticated servers (LM Studio, Ollama, ...)
don't need one.

**The gateway URL, model, and artifact threshold are remembered automatically** —
passing `--gateway-url`, `--model`, and/or `--artifact-threshold` on the command
line, or picking a model via `/models` in the TUI, writes them to `config.toml`
so a bare `pcli` (no flags) picks them up next time. The API key is deliberately
never auto-written to disk; set it via `PCLI_GATEWAY_API_KEY` (or `--api-key`
each time) if your gateway needs one.

Tool results longer than the artifact threshold (default 4000 chars) get
truncated out of the live conversation and archived to the artifact library —
the model calls `fetch_artifact` to page through the rest if it needs to.
Raise this if your model keeps needing to re-fetch large outputs; lower it to
keep context usage tighter.

Old conversation history gets the same treatment once it grows large: once
context usage crosses a threshold (default 80% of the model's context
limit), the oldest turns are auto-summarized and archived the same way, or
you can trigger it manually with `/compact` in the TUI — see
[`docs/configuration.md`](docs/configuration.md#auto-compaction).

Pass `--local-api` when pointed at a local/free OpenAI-compatible server (LM
Studio, Ollama, ...) to lift the tool-iteration and rate-limit guardrails and
force cost to $0 for that gateway — it's paired to (and persisted with)
whichever gateway is active, not a global switch. See
[`docs/configuration.md`](docs/configuration.md#local-api-mode) for details.

### LM Studio

1. In LM Studio, load a model and start the local server (Developer tab) — it
   defaults to `http://localhost:1234/v1`.
2. Point pcli at it. No API key is required:

   ```
   # PowerShell
   $env:PCLI_GATEWAY_URL = "http://localhost:1234/v1"
   $env:PCLI_MODEL = "<model id from LM Studio>"
   pcli
   ```

   or pass it inline: `pcli --gateway-url http://localhost:1234/v1 --model <model id>`.
   Either way, both values are saved to `config.toml`, so a plain `pcli` afterwards
   reconnects to the same gateway/model without needing the env vars or flags again.

3. Not sure of the exact model id LM Studio expects? Launch pcli and run
   `/models` — it lists whatever LM Studio reports on `GET /v1/models` and lets
   you pick one interactively (or run `/models <model-id>` to set it directly).
   This also saves the choice to `config.toml`.
