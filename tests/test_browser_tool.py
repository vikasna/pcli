"""Coverage for tools/builtin/browser_tool.py - the browser_* tool layer.
Uses a fake BrowserSession double (no real Playwright involved - see
test_browser_session.py for that layer) so these tests exercise the tools'
own argument handling, error formatting, and ToolContext wiring."""

from pathlib import Path

import pytest

from pcli.browser.session import BrowserUnavailableError
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.browser_tool import (
    BROWSER_CLICK,
    BROWSER_NAVIGATE,
    BROWSER_PRESS_KEY,
    BROWSER_READ_PAGE,
    BROWSER_SCREENSHOT,
    BROWSER_TYPE,
    BROWSER_WAIT_FOR,
)
from pcli.tools.registry import build_default_registry


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("browser_* tools should never touch the sandbox")


class _FakeBrowserSession:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._fail = fail
        self.current_url: str | None = "https://example.test/start"

    def _maybe_fail(self) -> None:
        if self._fail is not None:
            raise self._fail

    async def navigate(self, url: str) -> str:
        self.calls.append(("navigate", (url,)))
        self._maybe_fail()
        self.current_url = url
        return url

    async def click(self, selector: str) -> None:
        self.calls.append(("click", (selector,)))
        self._maybe_fail()

    async def type_text(self, selector: str, text: str) -> None:
        self.calls.append(("type_text", (selector, text)))
        self._maybe_fail()

    async def press_key(self, key: str) -> None:
        self.calls.append(("press_key", (key,)))
        self._maybe_fail()

    async def wait_for(self, selector: str, timeout_s: float) -> None:
        self.calls.append(("wait_for", (selector, timeout_s)))
        self._maybe_fail()

    async def read_page(self) -> str:
        self.calls.append(("read_page", ()))
        self._maybe_fail()
        return "Welcome to the page."

    async def screenshot(self, path: Path) -> None:
        self.calls.append(("screenshot", (path,)))
        self._maybe_fail()


def _ctx(tmp_path: Path, *, browser_session=None) -> ToolContext:
    return ToolContext(
        sandbox=_NullSandbox(),
        guardrails=GuardrailsConfig(),
        cwd=tmp_path,
        browser_session=browser_session,
    )


# --- registry ---


def test_browser_tools_are_registered_by_default():
    registry = build_default_registry()
    for name in (
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_wait_for",
        "browser_read_page",
        "browser_screenshot",
    ):
        assert name in registry


def test_mutating_browser_tools_need_permission_and_are_not_plan_mode_safe():
    for tool in (
        BROWSER_NAVIGATE,
        BROWSER_CLICK,
        BROWSER_TYPE,
        BROWSER_PRESS_KEY,
        BROWSER_WAIT_FOR,
    ):
        assert tool.needs_permission is True, tool.name
        assert tool.plan_mode_safe is False, tool.name


def test_read_only_browser_tools_need_no_permission_and_are_plan_mode_safe():
    for tool in (BROWSER_READ_PAGE, BROWSER_SCREENSHOT):
        assert tool.needs_permission is False, tool.name
        assert tool.plan_mode_safe is True, tool.name


# --- no browser session configured (e.g. a caller that built a bare ToolContext) ---


@pytest.mark.asyncio
async def test_each_tool_reports_a_clean_error_with_no_browser_session(tmp_path: Path):
    ctx = _ctx(tmp_path, browser_session=None)
    cases = [
        (BROWSER_NAVIGATE, {"url": "https://example.test"}),
        (BROWSER_CLICK, {"selector": "#go"}),
        (BROWSER_TYPE, {"selector": "#field", "text": "hi"}),
        (BROWSER_PRESS_KEY, {"key": "Enter"}),
        (BROWSER_WAIT_FOR, {"selector": "#done"}),
        (BROWSER_READ_PAGE, {}),
        (BROWSER_SCREENSHOT, {}),
    ]
    for tool, arguments in cases:
        result = await tool.handler(arguments, ctx)
        assert result.is_error is True, tool.name
        assert "no browser session configured" in result.output


# --- happy paths ---


@pytest.mark.asyncio
async def test_browser_navigate_reports_the_landed_url(tmp_path: Path):
    session = _FakeBrowserSession()
    result = await BROWSER_NAVIGATE.handler({"url": "https://example.test/page"}, _ctx(tmp_path, browser_session=session))
    assert result.is_error is False
    assert "https://example.test/page" in result.output
    assert session.calls == [("navigate", ("https://example.test/page",))]


@pytest.mark.asyncio
async def test_browser_click_reports_the_selector(tmp_path: Path):
    session = _FakeBrowserSession()
    result = await BROWSER_CLICK.handler({"selector": "text=Sign in"}, _ctx(tmp_path, browser_session=session))
    assert result.is_error is False
    assert "text=Sign in" in result.output
    assert session.calls == [("click", ("text=Sign in",))]


@pytest.mark.asyncio
async def test_browser_type_fills_the_field(tmp_path: Path):
    session = _FakeBrowserSession()
    result = await BROWSER_TYPE.handler(
        {"selector": "#user", "text": "alice"}, _ctx(tmp_path, browser_session=session)
    )
    assert result.is_error is False
    assert session.calls == [("type_text", ("#user", "alice"))]


@pytest.mark.asyncio
async def test_browser_press_key_sends_the_key(tmp_path: Path):
    session = _FakeBrowserSession()
    result = await BROWSER_PRESS_KEY.handler({"key": "Enter"}, _ctx(tmp_path, browser_session=session))
    assert result.is_error is False
    assert session.calls == [("press_key", ("Enter",))]


@pytest.mark.asyncio
async def test_browser_wait_for_uses_the_default_timeout_when_not_given(tmp_path: Path):
    session = _FakeBrowserSession()
    result = await BROWSER_WAIT_FOR.handler({"selector": "#done"}, _ctx(tmp_path, browser_session=session))
    assert result.is_error is False
    assert session.calls == [("wait_for", ("#done", 10.0))]


@pytest.mark.asyncio
async def test_browser_wait_for_honors_an_explicit_timeout(tmp_path: Path):
    session = _FakeBrowserSession()
    await BROWSER_WAIT_FOR.handler(
        {"selector": "#done", "timeout_s": 30}, _ctx(tmp_path, browser_session=session)
    )
    assert session.calls == [("wait_for", ("#done", 30.0))]


@pytest.mark.asyncio
async def test_browser_read_page_includes_the_current_url_and_text(tmp_path: Path):
    session = _FakeBrowserSession()
    session.current_url = "https://example.test/dashboard"
    result = await BROWSER_READ_PAGE.handler({}, _ctx(tmp_path, browser_session=session))
    assert result.is_error is False
    assert "https://example.test/dashboard" in result.output
    assert "Welcome to the page." in result.output


@pytest.mark.asyncio
async def test_browser_screenshot_saves_under_the_screenshots_dir_and_reports_the_path(
    tmp_path: Path,
):
    from pcli.config.paths import browser_screenshots_dir

    session = _FakeBrowserSession()
    result = await BROWSER_SCREENSHOT.handler({}, _ctx(tmp_path, browser_session=session))

    assert result.is_error is False
    assert session.calls[0][0] == "screenshot"
    saved_path = session.calls[0][1][0]
    assert saved_path.parent == browser_screenshots_dir()
    assert saved_path.suffix == ".png"
    assert str(saved_path) in result.output


# --- BrowserUnavailableError surfaces a friendly, actionable message ---


@pytest.mark.asyncio
async def test_navigate_surfaces_browser_unavailable_with_an_install_suggestion(tmp_path: Path):
    session = _FakeBrowserSession(fail=BrowserUnavailableError("Chromium isn't downloaded yet."))
    result = await BROWSER_NAVIGATE.handler(
        {"url": "https://example.test"}, _ctx(tmp_path, browser_session=session)
    )
    assert result.is_error is True
    assert "Chromium isn't downloaded yet." in result.output
    assert "playwright install chromium" in result.output


@pytest.mark.asyncio
async def test_read_page_surfaces_browser_unavailable_with_an_install_suggestion(tmp_path: Path):
    session = _FakeBrowserSession(fail=BrowserUnavailableError("Browser tools need the extra."))
    result = await BROWSER_READ_PAGE.handler({}, _ctx(tmp_path, browser_session=session))
    assert result.is_error is True
    assert "Browser tools need the extra." in result.output
    assert "pip install" in result.output
