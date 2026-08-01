"""LLM-driven tool schema synthesis from raw --help text, for software
without a curated plugin. Strictly schema-validated before being trusted;
retries once with the validation error fed back if the first attempt
doesn't parse/validate, then gives up rather than registering something
malformed."""

from __future__ import annotations

import json

import jsonschema

from pcli.llm.client import GatewayClient
from pcli.llm.models import ChatMessage

SYNTHESIZED_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "description": {"type": "string"},
                    "subcommand": {"type": "array", "items": {"type": "string"}},
                    "parameters": {"type": "object"},
                    "risk": {"type": "string", "enum": ["read", "mutate", "destructive"]},
                },
                "required": ["name", "description", "subcommand", "parameters", "risk"],
            },
        }
    },
    "required": ["tools"],
}

_SYSTEM_PROMPT = """You turn raw CLI --help output into a JSON list of tool definitions for \
an AI agent to call. Rules:
- Each tool maps to ONE specific subcommand invocation, given as an argv list in \
"subcommand" (e.g. ["get", "pods"], or [] if the binary takes flags directly with no \
subcommand).
- "parameters" is a JSON Schema object (with "type": "object" and "properties") describing \
the subcommand's *optional/value flags* as named properties (e.g. "namespace", "output"), \
NOT positional words already baked into "subcommand". Use "type": "boolean" for flags that \
take no value. Property names must be valid identifiers (letters, digits, underscore).
- "risk" must be "read" for read-only/inspection commands, "mutate" for commands that change \
state, "destructive" for commands that delete/terminate/irreversibly change state. When in \
doubt, choose the more cautious (destructive over mutate, mutate over read).
- Only include commands you are reasonably confident about from the help text. Fewer, \
correct tools are better than many guessed ones.
- Respond with ONLY a JSON object of the shape {"tools": [...]}, no prose, no code fences."""


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


async def synthesize_tools(
    client: GatewayClient,
    *,
    software_name: str,
    help_corpus: str,
    model: str | None = None,
) -> list[dict]:
    messages = [
        ChatMessage(role="system", content=_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=f"Software: {software_name}\n\n--help output:\n\n{help_corpus[:12000]}",
        ),
    ]

    last_error: Exception | None = None
    for attempt in range(2):
        message, _usage = await client.collect(messages, model=model)
        raw = _strip_code_fence((message.content or "").strip())
        try:
            data = json.loads(raw)
            jsonschema.validate(data, SYNTHESIZED_TOOL_SCHEMA)
            return data["tools"]
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last_error = exc
            if attempt == 0:
                messages.append(ChatMessage(role="assistant", content=raw))
                messages.append(
                    ChatMessage(
                        role="user",
                        content=f"That wasn't valid: {exc}. Respond again with ONLY the "
                        "corrected JSON object.",
                    )
                )

    raise ValueError(f"Could not synthesize a valid tool schema for '{software_name}': {last_error}")
