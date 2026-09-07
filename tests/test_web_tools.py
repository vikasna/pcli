"""Coverage for web_fetch/web_search — pcli's only tools with real internet
access. Network calls are respx-mocked throughout; test_web_tools_live.py
(separate, not run by default) hits the real endpoints."""

from pathlib import Path

import httpx
import pytest
import respx

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.web_tools import WEB_FETCH, WEB_SEARCH, _html_to_text
from pcli.tools.registry import build_default_registry


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("web_fetch/web_search should never touch the sandbox")


def _ctx(tmp_path: Path, *, brave_search_api_key: str = "") -> ToolContext:
    return ToolContext(
        sandbox=_NullSandbox(),
        guardrails=GuardrailsConfig(),
        cwd=tmp_path,
        brave_search_api_key=brave_search_api_key,
    )


# --- _html_to_text ---


def test_html_to_text_strips_tags_and_scripts():
    html = "<html><body><script>evil()</script><p>Hello</p><p>World</p></body></html>"
    text = _html_to_text(html)
    assert "evil()" not in text
    assert "Hello" in text
    assert "World" in text


def test_html_to_text_inserts_newlines_at_block_boundaries():
    html = "<div>line one</div><div>line two</div>"
    text = _html_to_text(html)
    assert text.splitlines() == ["line one", "line two"]


# --- web_fetch ---


@pytest.mark.asyncio
@respx.mock
async def test_web_fetch_converts_html_to_text(tmp_path: Path):
    respx.get("https://example.test/page").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><body><h1>Title</h1><p>Some content.</p></body></html>",
        )
    )
    result = await WEB_FETCH.handler({"url": "https://example.test/page"}, _ctx(tmp_path))
    assert result.is_error is False
    assert "Title" in result.output
    assert "Some content." in result.output
    assert "<p>" not in result.output


@pytest.mark.asyncio
@respx.mock
async def test_web_fetch_returns_json_as_is(tmp_path: Path):
    respx.get("https://example.test/data.json").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"a": 1}'
        )
    )
    result = await WEB_FETCH.handler({"url": "https://example.test/data.json"}, _ctx(tmp_path))
    assert result.is_error is False
    assert result.output == '{"a": 1}'


@pytest.mark.asyncio
@respx.mock
async def test_web_fetch_rejects_binary_content(tmp_path: Path):
    respx.get("https://example.test/image.png").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    )
    result = await WEB_FETCH.handler({"url": "https://example.test/image.png"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "image/png" in result.output
    assert "[pcli] Suggestion" in result.output


@pytest.mark.asyncio
@respx.mock
async def test_web_fetch_truncates_to_max_chars(tmp_path: Path):
    respx.get("https://example.test/long").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 1000)
    )
    result = await WEB_FETCH.handler(
        {"url": "https://example.test/long", "max_chars": 100}, _ctx(tmp_path)
    )
    assert result.is_error is False
    assert "[...truncated to 100 chars...]" in result.output
    assert len(result.output) < 1000


@pytest.mark.asyncio
@respx.mock
async def test_web_fetch_reports_http_error_status(tmp_path: Path):
    respx.get("https://example.test/missing").mock(return_value=httpx.Response(404))
    result = await WEB_FETCH.handler({"url": "https://example.test/missing"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "404" in result.output
    assert "[pcli] Suggestion" in result.output


@pytest.mark.asyncio
@respx.mock
async def test_web_fetch_reports_a_clear_error_on_timeout(tmp_path: Path):
    respx.get("https://example.test/slow").mock(side_effect=httpx.TimeoutException("timed out"))
    result = await WEB_FETCH.handler({"url": "https://example.test/slow"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "timed out" in result.output
    assert "[pcli] Suggestion" in result.output


# --- web_search: DuckDuckGo fallback (no API key configured) ---

_DDG_HTML_TWO_RESULTS = """
<html><body>
<div class="result results_links results_links_deep result--ad ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a class="result__a" href="https://ads.example/x">An ad</a></h2>
    <a class="result__snippet" href="https://ads.example/x">Sponsored content</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://pypi.org/project/requests/">requests - PyPI</a></h2>
    <a class="result__snippet" href="https://pypi.org/project/requests/">Python HTTP for Humans.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://docs.python-requests.org/">Requests docs</a></h2>
    <a class="result__snippet" href="https://docs.python-requests.org/">Official documentation.</a>
  </div>
</div>
</body></html>
"""


@pytest.mark.asyncio
@respx.mock
async def test_web_search_falls_back_to_duckduckgo_without_an_api_key(tmp_path: Path):
    route = respx.post("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(200, content=_DDG_HTML_TWO_RESULTS.encode())
    )
    result = await WEB_SEARCH.handler({"query": "python requests library"}, _ctx(tmp_path))

    assert result.is_error is False
    assert "requests - PyPI" in result.output
    assert "https://pypi.org/project/requests/" in result.output
    assert "Requests docs" in result.output
    # The ad block must be skipped - only organic (web-result) entries.
    assert "An ad" not in result.output
    assert "ads.example" not in result.output
    sent = route.calls.last.request
    assert sent.headers["user-agent"]  # a browser-like UA was set, not httpx's default
    assert b"python+requests+library" in sent.content or b"query" not in sent.content


@pytest.mark.asyncio
@respx.mock
async def test_web_search_respects_max_results(tmp_path: Path):
    respx.post("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(200, content=_DDG_HTML_TWO_RESULTS.encode())
    )
    result = await WEB_SEARCH.handler(
        {"query": "python requests library", "max_results": 1}, _ctx(tmp_path)
    )
    assert result.is_error is False
    assert "requests - PyPI" in result.output
    assert "Requests docs" not in result.output


@pytest.mark.asyncio
@respx.mock
async def test_web_search_reports_no_results_cleanly(tmp_path: Path):
    respx.post("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(200, content=b"<html><body>no matches</body></html>")
    )
    result = await WEB_SEARCH.handler({"query": "asdkjfhaslkdjfhas"}, _ctx(tmp_path))
    assert result.is_error is False
    assert result.output == "No results."


@pytest.mark.asyncio
@respx.mock
async def test_web_search_reports_a_clear_error_on_network_failure(tmp_path: Path):
    respx.post("https://html.duckduckgo.com/html/").mock(side_effect=httpx.ConnectError("boom"))
    result = await WEB_SEARCH.handler({"query": "anything"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "[pcli] Suggestion" in result.output
    assert "brave_search_api_key" in result.output


# --- web_search: Brave API (used when configured) ---


@pytest.mark.asyncio
@respx.mock
async def test_web_search_uses_brave_when_api_key_is_configured(tmp_path: Path):
    ddg_route = respx.post("https://html.duckduckgo.com/html/")
    ddg_route.mock(return_value=httpx.Response(200, content=_DDG_HTML_TWO_RESULTS.encode()))
    brave_route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "Brave Result", "url": "https://brave.example/1", "description": "d1"}
                    ]
                }
            },
        )
    )

    result = await WEB_SEARCH.handler(
        {"query": "anything"}, _ctx(tmp_path, brave_search_api_key="secret-key")
    )

    assert result.is_error is False
    assert "Brave Result" in result.output
    assert "https://brave.example/1" in result.output
    assert brave_route.called
    assert not ddg_route.called  # Brave used instead of the fallback, not alongside it
    assert brave_route.calls.last.request.headers["x-subscription-token"] == "secret-key"


# --- registration ---


def test_web_fetch_and_web_search_are_registered_in_the_default_registry():
    registry = build_default_registry()
    assert registry.get("web_fetch") is not None
    assert registry.get("web_search") is not None


def test_web_fetch_and_web_search_need_permission():
    """Both make outbound network requests the model controls the target
    of (arbitrary URL / arbitrary query) - same risk class as download_file."""
    assert WEB_FETCH.needs_permission is True
    assert WEB_SEARCH.needs_permission is True


def test_web_fetch_and_web_search_are_plan_mode_safe():
    """Both are read-only (no local filesystem/system mutation) - safe to
    keep available during plan mode, same as read_file."""
    assert WEB_FETCH.plan_mode_safe is True
    assert WEB_SEARCH.plan_mode_safe is True
