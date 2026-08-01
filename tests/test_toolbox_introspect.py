import json

import httpx
import pytest
import respx

import pcli.tools.toolbox.introspect as introspect_module
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.sandbox.base import ExecResult
from pcli.tools.toolbox.synthesize import synthesize_tools


class _FakeSandbox:
    def __init__(self, responses: dict[tuple, str]) -> None:
        self._responses = responses

    async def execute(self, request):
        key = tuple(request.command[1:])
        return ExecResult(
            stdout=self._responses.get(key, ""),
            stderr="",
            exit_code=0,
            timed_out=False,
            backend_used="fake",
        )


def test_guess_subcommands_extracts_indented_words():
    help_text = (
        "Usage: tool [command]\n\n"
        "Commands:\n"
        "  status    Show status\n"
        "  restart   Restart the service\n"
        "\n"
        "not-a-command line without leading spaces\n"
    )
    assert introspect_module._guess_subcommands(help_text) == ["status", "restart"]


@pytest.mark.asyncio
async def test_collect_help_corpus_walks_guessed_subcommands(tmp_path, monkeypatch):
    top_help = "Usage: widget [command]\n\nCommands:\n  list      List widgets\n  create    Create a widget\n"
    responses = {
        ("--help",): top_help,
        ("list", "--help"): "Usage: widget list [--all]\n",
        ("create", "--help"): "Usage: widget create <name>\n",
    }
    monkeypatch.setattr(
        introspect_module, "RestrictedSubprocessSandbox", lambda **kw: _FakeSandbox(responses)
    )

    corpus = await introspect_module.collect_help_corpus("/usr/bin/widget", tmp_path)
    assert "widget --help" in corpus
    assert "widget list --help" in corpus
    assert "widget create --help" in corpus
    assert "List widgets" in corpus


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="k",
        default_model="fake-model",
        max_retries=1,
    )


def _sse(text: str) -> bytes:
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    return (f"data: {json.dumps(chunk)}\n\n" + "data: [DONE]\n\n").encode()


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_tools_valid_response():
    valid = {
        "tools": [
            {
                "name": "list_widgets",
                "description": "List widgets.",
                "subcommand": ["list"],
                "parameters": {"type": "object", "properties": {"all": {"type": "boolean"}}},
                "risk": "read",
            }
        ]
    }
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(json.dumps(valid)))
    )
    async with GatewayClient(_settings()) as client:
        tools = await synthesize_tools(client, software_name="widget", help_corpus="Usage: widget list")
    assert tools == valid["tools"]


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_tools_retries_once_then_succeeds():
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, content=_sse("not json at all")),
        httpx.Response(200, content=_sse(json.dumps({"tools": []}))),
    ]
    async with GatewayClient(_settings()) as client:
        tools = await synthesize_tools(client, software_name="widget", help_corpus="...")
    assert tools == []
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_tools_gives_up_after_two_invalid_attempts():
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse("still not json"))
    )
    async with GatewayClient(_settings()) as client:
        with pytest.raises(ValueError):
            await synthesize_tools(client, software_name="widget", help_corpus="...")


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_tools_rejects_schema_violating_response():
    # Missing required "risk" field.
    bad = {
        "tools": [
            {
                "name": "list_widgets",
                "description": "List widgets.",
                "subcommand": ["list"],
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    }
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse(json.dumps(bad)))
    )
    async with GatewayClient(_settings()) as client:
        with pytest.raises(ValueError):
            await synthesize_tools(client, software_name="widget", help_corpus="...")
