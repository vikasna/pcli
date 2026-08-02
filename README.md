# pcli

An opencode-style AI coding agent CLI/TUI for Python. Connects to any OpenAI-compatible LLM gateway, with session management, cost tracking, permission handling, guardrails, tiered sandboxed execution, a lazy-loading Python discovery tool, and a toolbox for discovering and using installed OS software (kubectl, SGE, Kafka, httpd, ...).

See `requirements.md` for the original project brief.

## Development

```
pip install -e ".[dev,docker,win]"
pcli
```

## Configuring the gateway

pcli talks to any OpenAI-compatible chat-completions endpoint. Configure it via
environment variables, CLI flags, or `config.toml` (see `pcli.config.paths.config_file()`
for its location) — precedence is CLI flags > env vars > `config.toml` > defaults.

| Setting | Env var | CLI flag | config.toml key |
|---|---|---|---|
| Base URL | `PCLI_GATEWAY_URL` | `--gateway-url` | `gateway_base_url` |
| API key (optional) | `PCLI_GATEWAY_API_KEY` | `--api-key` | `gateway_api_key` |
| Default model | `PCLI_MODEL` | `--model` | `default_model` |

The API key is optional — local, unauthenticated servers (LM Studio, Ollama, ...)
don't need one.

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

3. Not sure of the exact model id LM Studio expects? Launch pcli and run
   `/models` — it lists whatever LM Studio reports on `GET /v1/models` and lets
   you pick one interactively (or run `/models <model-id>` to set it directly).
