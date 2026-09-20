"""Coverage for browser/session.py's BrowserSession - the lazy Playwright
wrapper behind the browser_* tools (see test_browser_tool.py for the tool
layer itself, which uses a fake BrowserSession rather than touching
Playwright at all).

Playwright is genuinely not installed in this dev/CI environment (it's the
optional "browser" extra - see pyproject.toml) - the "unavailable" tests
below rely on that being true rather than mocking anything, and are skipped
if it ever is installed. The "launches once, reuses the page, closes
cleanly" tests inject a minimal fake playwright.async_api module via
sys.modules so BrowserSession's own control flow (not Playwright's
internals) is exercised without needing the real package."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from pcli.browser.session import BrowserSession, BrowserUnavailableError


def _playwright_installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


class _FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = False
        self.new_page_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext, *, fail_with: Exception | None = None) -> None:
        self._context = context
        self._fail_with = fail_with
        self.launch_calls: list[tuple[str, bool]] = []

    async def launch_persistent_context(self, user_data_dir: str, *, headless: bool):
        self.launch_calls.append((user_data_dir, headless))
        if self._fail_with is not None:
            raise self._fail_with
        return self._context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakePlaywrightContextManager:
    """What async_playwright() returns - a plain object with an async start()."""

    def __init__(self, playwright_obj: _FakePlaywright) -> None:
        self._playwright_obj = playwright_obj

    async def start(self) -> _FakePlaywright:
        return self._playwright_obj


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, chromium: _FakeChromium) -> _FakePlaywright:
    playwright_obj = _FakePlaywright(chromium)
    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = lambda: _FakePlaywrightContextManager(playwright_obj)  # type: ignore[attr-defined]
    fake_playwright_pkg = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    return playwright_obj


# --- construction: cheap, no launch ---


def test_construction_does_not_start_a_browser():
    session = BrowserSession()
    assert session.is_started is False
    assert session.current_url is None


# --- unavailable: real, unmocked (playwright genuinely not installed here) ---


@pytest.mark.skipif(_playwright_installed(), reason="playwright is installed in this environment")
@pytest.mark.asyncio
async def test_ensure_page_raises_friendly_error_when_playwright_not_installed():
    session = BrowserSession()
    with pytest.raises(BrowserUnavailableError, match="browser.*extra"):
        await session._ensure_page()


# --- lazy launch / reuse / close, via a fake playwright.async_api ---


@pytest.mark.asyncio
async def test_ensure_page_launches_once_and_reuses_the_same_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    context = _FakeContext()
    chromium = _FakeChromium(context)
    _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession(profile="default", headless=True)
    page1 = await session._ensure_page()
    page2 = await session._ensure_page()

    assert page1 is page2
    assert session.is_started is True
    assert len(chromium.launch_calls) == 1  # only launched once, not per call
    user_data_dir, headless = chromium.launch_calls[0]
    assert "browser_profiles" in user_data_dir
    assert "default" in user_data_dir
    assert headless is True


@pytest.mark.asyncio
async def test_ensure_page_uses_an_existing_persisted_tab_if_one_is_already_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A persistent context can come back with an already-open tab (e.g. a
    restored session) - new_page() should only be called when there isn't
    one, not unconditionally."""
    context = _FakeContext()
    context.pages.append(_FakePage("https://example.test/restored"))
    chromium = _FakeChromium(context)
    _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession()
    page = await session._ensure_page()

    assert page.url == "https://example.test/restored"
    assert context.new_page_calls == 0


@pytest.mark.asyncio
async def test_close_tears_down_context_and_playwright(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    context = _FakeContext()
    chromium = _FakeChromium(context)
    playwright_obj = _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession()
    await session._ensure_page()

    await session.close()

    assert context.closed is True
    assert playwright_obj.stopped is True
    assert session.is_started is False


@pytest.mark.asyncio
async def test_close_is_a_noop_when_never_started():
    session = BrowserSession()
    await session.close()  # must not raise


@pytest.mark.asyncio
async def test_ensure_page_after_close_relaunches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    context = _FakeContext()
    chromium = _FakeChromium(context)
    _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession()
    await session._ensure_page()
    await session.close()
    await session._ensure_page()

    assert len(chromium.launch_calls) == 2
    assert session.is_started is True


@pytest.mark.asyncio
async def test_launch_failure_with_missing_executable_gives_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    chromium = _FakeChromium(_FakeContext(), fail_with=RuntimeError("Executable doesn't exist at ..."))
    _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession()
    with pytest.raises(BrowserUnavailableError, match="playwright install chromium"):
        await session._ensure_page()
    assert session.is_started is False


@pytest.mark.asyncio
async def test_launch_failure_with_an_unrelated_error_propagates_as_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    chromium = _FakeChromium(_FakeContext(), fail_with=RuntimeError("some other launch failure"))
    _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession()
    with pytest.raises(RuntimeError, match="some other launch failure"):
        await session._ensure_page()


@pytest.mark.asyncio
async def test_navigate_click_type_press_key_wait_for_read_page_delegate_to_the_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A thin integration check that each public method actually reaches
    the underlying page object with the right arguments - real Playwright
    Locator/Page behavior itself isn't reimplemented here, just the plumbing."""

    class _RecordingPage(_FakePage):
        def __init__(self) -> None:
            super().__init__("https://example.test/start")
            self.goto_calls: list[tuple[str, str]] = []
            self.locator_calls: list[str] = []
            self.pressed_keys: list[str] = []
            self.filled: list[tuple[str, str]] = []
            self.clicked: list[str] = []
            self.waited: list[tuple[str, float]] = []
            self.screenshot_paths: list[str] = []

        async def goto(self, url: str, *, wait_until: str) -> None:
            self.goto_calls.append((url, wait_until))
            self.url = url

        def locator(self, selector: str):
            self.locator_calls.append(selector)
            return _RecordingLocator(self, selector)

        async def inner_text(self, selector: str) -> str:
            return f"text of {selector}"

        async def screenshot(self, *, path: str) -> None:
            self.screenshot_paths.append(path)

        class _Keyboard:
            def __init__(self, page: _RecordingPage) -> None:
                self._page = page

            async def press(self, key: str) -> None:
                self._page.pressed_keys.append(key)

        @property
        def keyboard(self):
            return _RecordingPage._Keyboard(self)

    class _RecordingLocator:
        def __init__(self, page: _RecordingPage, selector: str) -> None:
            self._page = page
            self._selector = selector

        @property
        def first(self):
            return self

        async def click(self) -> None:
            self._page.clicked.append(self._selector)

        async def fill(self, text: str) -> None:
            self._page.filled.append((self._selector, text))

        async def wait_for(self, *, timeout: float) -> None:
            self._page.waited.append((self._selector, timeout))

    context = _FakeContext()
    context.pages.append(_RecordingPage())
    chromium = _FakeChromium(context)
    _install_fake_playwright(monkeypatch, chromium)

    session = BrowserSession()
    landed = await session.navigate("https://example.test/page")
    await session.click("#go")
    await session.type_text("#field", "hello")
    await session.press_key("Enter")
    await session.wait_for("#done", 5)
    text = await session.read_page()
    shot_path = tmp_path / "shot.png"
    await session.screenshot(shot_path)

    page = context.pages[0]
    assert landed == "https://example.test/page"
    assert page.goto_calls == [("https://example.test/page", "load")]
    assert page.clicked == ["#go"]
    assert page.filled == [("#field", "hello")]
    assert page.pressed_keys == ["Enter"]
    assert page.waited == [("#done", 5000)]  # seconds -> ms
    assert text == "text of body"
    assert page.screenshot_paths == [str(shot_path)]
    assert session.current_url == "https://example.test/page"
