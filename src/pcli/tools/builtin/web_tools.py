"""web_fetch/web_search: pcli's only tools with real internet access (every
other tool is local — filesystem, shell, a configured LLM gateway). Built on
httpx (already a pcli dependency) plus stdlib html.parser, no new
dependencies.

web_search has no built-in, keyless "the" web search API — there isn't one.
If Settings.brave_search_api_key is configured, it's used (a real, supported
API). Otherwise this falls back to scraping DuckDuckGo's server-rendered
HTML results page (https://html.duckduckgo.com/html/), which needs no API
key and works out of the box, but is inherently best-effort: it depends on
DuckDuckGo's current HTML structure and a browser-like User-Agent (confirmed
necessary — httpx's default UA gets a soft-blocked empty response), and
could break if either changes. Treat it as a reasonable default, not a
guarantee — recommend brave_search_api_key for anything load-bearing.

Also confirmed directly against this endpoint: search operators
(site:/filetype:/-exclusion/OR/intitle:) don't work here — a query using
any of them comes back as a soft-blocked empty response (HTTP 202, no
results), not just a degraded/literal-text match. Only plain keywords and
quoted exact phrases reliably return results. This is why WEB_SEARCH's own
tool description steers the model away from operators rather than assuming
they're safe to try.
"""

from __future__ import annotations

from html.parser import HTMLParser

import httpx

from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_DEFAULT_TIMEOUT_S = 20.0
_MAX_FETCH_BYTES = 5_000_000  # safety cap on what we'll read into memory
_DEFAULT_MAX_CHARS = 8_000
_DEFAULT_MAX_RESULTS = 5

# DuckDuckGo's HTML endpoint soft-blocks (returns the homepage, not results)
# for a generic/missing User-Agent - confirmed directly, not guessed.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


class _TextExtractor(HTMLParser):
    """Best-effort HTML->text: drops script/style content, inserts a
    newline at block-level element boundaries, keeps everything else.
    Doesn't attempt readability-style boilerplate removal (nav/footer/ads
    stay in) - good enough for "read what this page says", not a
    replacement for a real readability extractor."""

    _BLOCK_TAGS = frozenset(
        {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )
    _SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()


async def _web_fetch(arguments: dict, ctx: ToolContext) -> ToolResult:
    url = arguments["url"]
    max_chars = int(arguments.get("max_chars", _DEFAULT_MAX_CHARS))

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_DEFAULT_TIMEOUT_S) as client:
            response = await client.get(url)
    except httpx.TimeoutException:
        return ToolResult(
            output=f"Fetch timed out after {_DEFAULT_TIMEOUT_S:g}s for {url}\n"
            "[pcli] Suggestion: the site may be slow or unreachable from here - try again, or "
            "skip it and use another source.",
            is_error=True,
        )
    except httpx.HTTPError as exc:
        return ToolResult(
            output=f"Fetch failed: {exc}\n"
            "[pcli] Suggestion: check the URL is correct and reachable, and that network access "
            "is actually available from this environment.",
            is_error=True,
        )

    if response.status_code >= 400:
        return ToolResult(
            output=f"Fetch failed: HTTP {response.status_code} for {url}\n"
            "[pcli] Suggestion: double-check the URL is correct and still live - a 404/403 won't "
            "resolve by retrying the identical URL.",
            is_error=True,
        )

    content_type = response.headers.get("content-type", "")
    raw_bytes = response.content
    if len(raw_bytes) > _MAX_FETCH_BYTES:
        return ToolResult(
            output=f"Response exceeded the {_MAX_FETCH_BYTES:,}-byte safety cap for {url} "
            f"(content-type: {content_type or 'unknown'}).\n"
            "[pcli] Suggestion: this looks like a large binary/media file, not a page to read - "
            "use download_file instead if you actually need the raw content on disk.",
            is_error=True,
        )

    text = raw_bytes.decode(errors="replace")
    if "html" in content_type:
        text = _html_to_text(text)
    elif not content_type.startswith(("text/", "application/json")) and content_type:
        return ToolResult(
            output=f"{url} is {content_type}, not text/HTML ({len(raw_bytes):,} bytes) - not "
            "something meaningful to read as text.\n"
            "[pcli] Suggestion: use download_file instead if you need this content on disk.",
            is_error=True,
        )

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[...truncated to {max_chars:,} chars...]"
    return ToolResult(output=text or "(empty response)")


WEB_FETCH = ToolSpec(
    name="web_fetch",
    description="Fetch a URL and return its readable text content (HTML is converted to plain "
    "text; JSON/plain text is returned as-is). Use this to actually read the content of a page "
    "you already have the URL for — e.g. one found via web_search, or one the user gave you. "
    "Not for downloading files to disk (use download_file for that).",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch."},
            "max_chars": {
                "type": "integer",
                "description": f"Maximum characters of text to return (default "
                f"{_DEFAULT_MAX_CHARS:,}).",
            },
        },
        "required": ["url"],
    },
    handler=_web_fetch,
    needs_permission=True,
    risk_description="Fetches content from a URL over the network.",
    plan_mode_safe=True,
)


class _DuckDuckGoResultParser(HTMLParser):
    """Parses DuckDuckGo's html.duckduckgo.com/html/ results page. Organic
    results are `<div class="... web-result ...">` (ads use `result--ad`
    instead and are skipped); within one, `<a class="result__a">` carries
    the title+URL and `<a class="result__snippet">` the snippet - confirmed
    directly against a real response, not guessed from memory."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._depth = 0
        self._result_div_depth: int | None = None
        self._current: dict[str, str] = {}
        self._capturing: str | None = None  # "title" | "snippet" | None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "div":
            self._depth += 1
            classes = (attrs_dict.get("class") or "").split()
            if self._result_div_depth is None and "web-result" in classes:
                self._result_div_depth = self._depth
                self._current = {"title": "", "url": "", "snippet": ""}
        elif tag == "a" and self._result_div_depth is not None:
            classes = (attrs_dict.get("class") or "").split()
            if "result__a" in classes:
                self._capturing = "title"
                self._current["url"] = attrs_dict.get("href") or ""
            elif "result__snippet" in classes:
                self._capturing = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._capturing = None
        elif tag == "div":
            if self._result_div_depth == self._depth:
                if self._current.get("title") and self._current.get("url"):
                    self.results.append(
                        {
                            "title": self._current["title"].strip(),
                            "url": self._current["url"],
                            "snippet": self._current["snippet"].strip(),
                        }
                    )
                self._result_div_depth = None
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capturing == "title":
            self._current["title"] = self._current["title"] + data
        elif self._capturing == "snippet":
            self._current["snippet"] = self._current["snippet"] + data


async def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client:
        response = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": _BROWSER_USER_AGENT},
        )
    response.raise_for_status()
    parser = _DuckDuckGoResultParser()
    parser.feed(response.text)
    return parser.results[:max_results]


async def _search_brave(query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
    response.raise_for_status()
    data = response.json()
    results = data.get("web", {}).get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in results[:max_results]
    ]


def _format_results(results: list[dict[str, str]]) -> str:
    if not results:
        return "No results."
    lines = []
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result['title']}\n   {result['url']}\n   {result['snippet']}")
    return "\n\n".join(lines)


async def _web_search(arguments: dict, ctx: ToolContext) -> ToolResult:
    query = arguments["query"]
    max_results = int(arguments.get("max_results", _DEFAULT_MAX_RESULTS))

    try:
        if ctx.brave_search_api_key:
            results = await _search_brave(query, max_results, ctx.brave_search_api_key)
        else:
            results = await _search_duckduckgo(query, max_results)
    except httpx.TimeoutException:
        return ToolResult(
            output=f"Search timed out after {_DEFAULT_TIMEOUT_S:g}s for query: {query}\n"
            "[pcli] Suggestion: try again, or narrow the query.",
            is_error=True,
        )
    except httpx.HTTPError as exc:
        return ToolResult(
            output=f"Search failed: {exc}\n"
            "[pcli] Suggestion: check network access is available from this environment. If "
            "this keeps failing, the no-API-key DuckDuckGo fallback may be getting blocked - "
            "configure brave_search_api_key for a real, supported search API instead.",
            is_error=True,
        )

    return ToolResult(output=_format_results(results))


WEB_SEARCH = ToolSpec(
    name="web_search",
    description="Search the web and return a short list of results (title, URL, snippet). Use "
    "web_fetch on a promising result's URL to read its actual content — search results alone "
    "are rarely enough to answer from. Uses a configured search API if available, otherwise a "
    "best-effort fallback with no setup required.\n\n"
    "Query tips: use short, keyword-based queries (roughly 2-6 words), not a full question or "
    "sentence — 'python asyncio cancel task' beats 'how do I cancel a task in python asyncio'. "
    "Quote an exact phrase you need verbatim (an error message, a function name, a title) — "
    "quoting is reliable on both backends. Avoid site:/filetype:/-exclusion/OR operators: they "
    "only work with a configured search API (Brave) and are unreliable — confirmed to return "
    "nothing, not just fewer results — on the no-setup DuckDuckGo fallback used by default. If "
    "you need a specific site, search normally and pick the matching result, or web_fetch a "
    "known URL directly instead. Start broad and re-search with narrower keywords based on what "
    "the first results show, rather than building one heavily-qualified query upfront.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": f"Maximum number of results to return (default "
                f"{_DEFAULT_MAX_RESULTS}).",
            },
        },
        "required": ["query"],
    },
    handler=_web_search,
    needs_permission=True,
    risk_description="Sends a search query to a third-party service over the network.",
    plan_mode_safe=True,
)
