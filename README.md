# pcli

An opencode-style AI coding agent CLI/TUI for Python. Connects to any OpenAI-compatible LLM gateway, with session management, cost tracking, permission handling, guardrails, tiered sandboxed execution, a lazy-loading Python discovery tool, and a toolbox for discovering and using installed OS software (kubectl, SGE, Kafka, httpd, ...).

See `requirements.md` for the original project brief.

## Development

```
pip install -e ".[dev,docker,win]"
pcli
```
