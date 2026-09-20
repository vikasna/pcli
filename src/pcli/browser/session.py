"""BrowserSession: a lazily-launched Playwright browser, wrapped for the
browser_* tools (tools/builtin/browser_tool.py).

Playwright (`pip install -e ".[browser]"`, then a one-time `playwright
install chromium`) is an optional dependency - this module must import
cleanly even when it isn't installed, so the actual `import playwright...`
only happens inside _ensure_page (first real use), not at module or
BrowserSession-construction time. A BrowserSession is cheap to construct
and hold onto even when nothing ever calls into it (see agent/runtime.py,
which always attaches one to AgentRuntime) - the browser process itself is
only started the first time a browser_* tool is actually called.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pcli.config.paths import browser_profiles_dir

if TYPE_CHECKING:
    # Optional dependency (the "browser" extra) - not installed in a plain
    # dev environment, so mypy can't resolve this even though it's
    # type-checking-only and never actually imported at runtime unless a
    # browser_* tool is actually called (see _ensure_page below).
    from playwright.async_api import Page  # type: ignore[import-not-found]

_INSTALL_HINT = (
    'Browser tools need the optional "browser" extra: run `pip install -e ".[browser]"`, then '
    "`playwright install chromium` once to download the browser itself."
)


class BrowserUnavailableError(Exception):
    """Raised when a browser_* tool is used but Playwright isn't installed,
    or Chromium hasn't been downloaded yet (`playwright install chromium`)."""


class BrowserSession:
    """Wraps one Playwright persistent BrowserContext and its single active
    Page. One instance is shared for the life of an AgentRuntime (agent/
    runtime.py) and threaded through every ToolContext built from it, so
    every browser_* call in a session reuses the same browser
    process/tab - navigating then clicking then reading stays in the same
    login/cookie state, the way a person's own browser session would. The
    profile directory (browser_profiles_dir) persists that state further,
    across separate pcli invocations too (e.g. separate scheduled `pcli
    run` runs), not just within one running session."""

    def __init__(self, *, profile: str = "default", headless: bool = False) -> None:
        self._profile = profile
        self._headless = headless
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Page | None = None

    @property
    def is_started(self) -> bool:
        return self._page is not None

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def current_url(self) -> str | None:
        return self._page.url if self._page is not None else None

    async def _ensure_page(self) -> Page:
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(_INSTALL_HINT) from exc

        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(browser_profiles_dir(self._profile)),
                headless=self._headless,
            )
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            if "Executable doesn't exist" in str(exc):
                raise BrowserUnavailableError(
                    "Chromium isn't downloaded yet - run `playwright install chromium` once."
                ) from exc
            raise
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        return self._page

    async def close(self) -> None:
        """Safe to call even if the browser was never started (nothing to
        close) - mirrors kill_all_background_jobs' own no-op-when-unused
        shape (sandbox/subprocess_backend.py), called the same way from
        ChatScreen.on_unmount / `pcli run`'s finally block."""
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def navigate(self, url: str) -> str:
        page = await self._ensure_page()
        await page.goto(url, wait_until="load")
        return page.url

    async def click(self, selector: str) -> None:
        page = await self._ensure_page()
        await page.locator(selector).first.click()

    async def type_text(self, selector: str, text: str) -> None:
        page = await self._ensure_page()
        await page.locator(selector).first.fill(text)

    async def press_key(self, key: str) -> None:
        page = await self._ensure_page()
        await page.keyboard.press(key)

    async def wait_for(self, selector: str, timeout_s: float) -> None:
        page = await self._ensure_page()
        await page.locator(selector).first.wait_for(timeout=timeout_s * 1000)

    async def read_page(self) -> str:
        page = await self._ensure_page()
        text: str = await page.inner_text("body")
        return text

    async def screenshot(self, path: Path) -> None:
        page = await self._ensure_page()
        await page.screenshot(path=str(path))
