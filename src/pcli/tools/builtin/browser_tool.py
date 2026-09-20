"""browser_*: drives a real Chromium browser (via Playwright, an optional
dependency - see pyproject.toml's "browser" extra and docs) so pcli can
operate a website the way a person would: click, type, wait for something
to appear, read what's on the page, see it via a screenshot. Distinct from
web_fetch/web_search (web_tools.py), which are HTTP-level and can't run
JavaScript, stay logged in, or interact with a page - reach for these only
when a site actually needs driving, not just reading.

Every call in a session shares one BrowserSession (ctx.browser_session, set
up once per AgentRuntime - see agent/runtime.py and browser/session.py) -
navigating, then clicking, then reading all happen in the same tab/login
state, the way a real browser session would.
"""

from __future__ import annotations

from pcli.browser.session import BrowserUnavailableError
from pcli.config.paths import browser_screenshots_dir
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.util.ids import new_id

_INSTALL_SUGGESTION = (
    '\n[pcli] Suggestion: install the optional browser extra and its browser binary once - '
    '`pip install -e ".[browser]"` then `playwright install chromium` - then try again.'
)


def _no_session_result() -> ToolResult:
    return ToolResult(
        output="Browser tools aren't available in this context (no browser session configured).",
        is_error=True,
    )


async def _browser_navigate(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    url = arguments["url"]
    try:
        landed_url = await ctx.browser_session.navigate(url)
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    return ToolResult(output=f"Navigated to {landed_url}")


BROWSER_NAVIGATE = ToolSpec(
    name="browser_navigate",
    description="Navigate the browser to a URL. Starts the browser on first use (a real, "
    "visible Chromium window in the TUI by default, or headless for a scheduled/unattended "
    "run). Use this before browser_click/browser_type/etc. - they act on whatever page is "
    "currently open.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "URL to navigate to."}},
        "required": ["url"],
    },
    handler=_browser_navigate,
    needs_permission=True,
    risk_description="Navigates the browser to a URL, which can trigger network requests, "
    "JavaScript execution, and login-state changes on a real website.",
    plan_mode_safe=False,
)


async def _browser_click(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    selector = arguments["selector"]
    try:
        await ctx.browser_session.click(selector)
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    return ToolResult(output=f"Clicked '{selector}'")


BROWSER_CLICK = ToolSpec(
    name="browser_click",
    description="Click an element on the currently open page, identified by a Playwright "
    "locator (CSS selector, or `text=...` to match visible text).",
    parameters={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Playwright locator for the element to click, e.g. "
                "'button#submit' or 'text=Sign in'.",
            }
        },
        "required": ["selector"],
    },
    handler=_browser_click,
    needs_permission=True,
    risk_description="Clicks an element on a real website - can submit a form, confirm a "
    "purchase, or otherwise take real action.",
    plan_mode_safe=False,
)


async def _browser_type(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    selector = arguments["selector"]
    text = arguments["text"]
    try:
        await ctx.browser_session.type_text(selector, text)
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    return ToolResult(output=f"Typed into '{selector}'")


BROWSER_TYPE = ToolSpec(
    name="browser_type",
    description="Type text into a field on the currently open page (replaces whatever was "
    "already in it), identified by a Playwright locator.",
    parameters={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Playwright locator for the field to type into.",
            },
            "text": {"type": "string", "description": "Text to type into the field."},
        },
        "required": ["selector", "text"],
    },
    handler=_browser_type,
    needs_permission=True,
    risk_description="Types text into a field on a real website - may enter real data "
    "(credentials, search queries, form input).",
    plan_mode_safe=False,
)


async def _browser_press_key(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    key = arguments["key"]
    try:
        await ctx.browser_session.press_key(key)
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    return ToolResult(output=f"Pressed '{key}'")


BROWSER_PRESS_KEY = ToolSpec(
    name="browser_press_key",
    description="Send a single keypress to the currently open page (e.g. 'Enter', 'Tab', "
    "'Escape') - useful for submitting a form after browser_type, or dismissing something.",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Key to press, in Playwright's key naming (e.g. 'Enter', 'Tab', "
                "'ArrowDown').",
            }
        },
        "required": ["key"],
    },
    handler=_browser_press_key,
    needs_permission=True,
    risk_description="Sends a keypress to a real website - can submit a form or trigger a "
    "keyboard shortcut.",
    plan_mode_safe=False,
)


async def _browser_wait_for(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    selector = arguments["selector"]
    timeout_s = float(arguments.get("timeout_s", 10))
    try:
        await ctx.browser_session.wait_for(selector, timeout_s)
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    return ToolResult(output=f"'{selector}' appeared")


BROWSER_WAIT_FOR = ToolSpec(
    name="browser_wait_for",
    description="Wait for an element to appear on the currently open page before continuing - "
    "use this after an action that loads content asynchronously, instead of guessing how long "
    "to wait.",
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Playwright locator to wait for."},
            "timeout_s": {
                "type": "number",
                "description": "Seconds to wait before giving up (default 10).",
            },
        },
        "required": ["selector"],
    },
    handler=_browser_wait_for,
    needs_permission=True,
    risk_description="Part of a browser automation sequence on a real website - gated the same "
    "as the actions it's waiting on behalf of.",
    plan_mode_safe=False,
)


async def _browser_read_page(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    try:
        text = await ctx.browser_session.read_page()
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    url = ctx.browser_session.current_url or "(unknown URL)"
    return ToolResult(output=f"--- {url} ---\n{text}" if text else f"--- {url} ---\n(empty page)")


BROWSER_READ_PAGE = ToolSpec(
    name="browser_read_page",
    description="Read the visible text of the currently open page - use this to see what's on "
    "the page after navigating or acting on it, the way you'd look at the screen yourself.",
    parameters={"type": "object", "properties": {}},
    handler=_browser_read_page,
    needs_permission=False,
    plan_mode_safe=True,
)


async def _browser_screenshot(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.browser_session is None:
        return _no_session_result()
    path = browser_screenshots_dir() / f"{new_id('shot_')}.png"
    try:
        await ctx.browser_session.screenshot(path)
    except BrowserUnavailableError as exc:
        return ToolResult(output=f"{exc}{_INSTALL_SUGGESTION}", is_error=True)
    url = ctx.browser_session.current_url or "(unknown URL)"
    return ToolResult(output=f"Screenshot of {url} saved to {path}")


BROWSER_SCREENSHOT = ToolSpec(
    name="browser_screenshot",
    description="Take a screenshot of the currently open page and save it to disk - use this "
    "to show the user what the page actually looks like, not just its text.",
    parameters={"type": "object", "properties": {}},
    handler=_browser_screenshot,
    needs_permission=False,
    plan_mode_safe=True,
)
