# Browser automation

`browser_*` (`src/pcli/tools/builtin/browser_tool.py`) drives a real
Chromium browser via [Playwright](https://playwright.dev/) so pcli can
operate a website the way a person would — click, type, wait for something
to appear, read what's on the page, see it via a screenshot. This is
distinct from `web_fetch`/`web_search` ([`tools.md`](tools.md#web_fetch)),
which are HTTP-level and can't run JavaScript, stay logged in, or interact
with a page at all — reach for these only when a site genuinely needs
driving, not just reading.

Implementation lives in `src/pcli/browser/session.py` (`BrowserSession`,
the Playwright wrapper) and `src/pcli/tools/builtin/browser_tool.py` (the
seven tools built on top of it).

## Installing

Playwright is an optional dependency — the `browser` extra
(`pyproject.toml`):

```
pip install -e ".[browser]"
playwright install chromium
```

Two separate steps, same precedent as the `docker` extra's "needs the
`docker` CLI on PATH" requirement ([`development.md`](development.md)):
`pip install -e ".[browser]"` only pulls in the Python `playwright`
package; Chromium itself is a separate ~300MB binary download, fetched
once with `playwright install chromium`. Nothing about installing pcli
itself requires either step — see [When the extra or Chromium isn't
installed](#when-the-extra-or-chromium-isnt-installed) below for what
happens if you skip them and a browser tool gets called anyway.

## `BrowserSession`: lazy launch, shared session, persistent profile

`BrowserSession` (`src/pcli/browser/session.py`) wraps one Playwright
persistent browser context and its single active page.

- **Lazily launched.** Constructing a `BrowserSession` is free — no browser
  process starts, and `import playwright` itself doesn't even happen,
  until a `browser_*` tool's first real call (`_ensure_page`). This is why
  pcli imports and runs fine with the `browser` extra not installed at all:
  nothing fails until a browser tool is actually used. `AgentRuntime`
  (`agent/runtime.py`) always constructs a `BrowserSession` unconditionally
  — there's no "if playwright is installed" branch — precisely because
  doing so costs nothing.
- **Shared for the whole life of a session.** One instance is threaded
  into every `ToolContext` built from a given `AgentRuntime`
  (`ToolContext.browser_session`), so every `browser_*` call within a
  session reuses the same browser process and tab — navigating, then
  clicking, then reading all happen in the same login/cookie state, the
  way a person's own browser session would.
- **Persistent profile, surviving across separate pcli invocations too.**
  The context is launched against a dedicated profile directory —
  `browser_profiles_dir(profile="default")` (`config/paths.py`), under
  pcli's data dir (`data_dir()/browser_profiles/<profile>`) — rather than
  a throwaway one. That's what makes cookies/login state survive not just
  within one running session, but across *separate* pcli invocations too:
  e.g. two separate scheduled `pcli run` runs a day apart pick up the same
  logged-in state the first run left behind.
- **Closed via `.close()`**, called from `ChatScreen.on_unmount` and from
  `pcli run`'s `finally` block (`src/pcli/cli.py`) — mirroring how the
  sandbox/gateway client are already cleaned up in both of those places.
  Safe to call even if the browser was never actually started.

## The seven tools

| Tool | Permission | `plan_mode_safe` |
|---|---|---|
| `browser_navigate(url)` | required | `False` |
| `browser_click(selector)` | required | `False` |
| `browser_type(selector, text)` | required | `False` |
| `browser_press_key(key)` | required | `False` |
| `browser_wait_for(selector, timeout_s=10)` | required | `False` |
| `browser_read_page()` | not required | `True` |
| `browser_screenshot()` | not required | `True` |

**The five action tools** (`navigate`/`click`/`type`/`press_key`/`wait_for`)
are all `needs_permission=True` and not `plan_mode_safe` — each is
consequential on a real website: navigating can trigger network requests
and login-state changes, clicking can submit a form or confirm a purchase,
typing can enter real data, a keypress can submit a form just as much as a
click can. `browser_wait_for` is gated the same way even though it's
"just waiting," since it's always part of a sequence acting on a real
site. Selectors use Playwright's own locator syntax directly — a CSS
selector, or `text=...` to match visible text — no custom mini-language on
top.

**The two read-only tools** (`browser_read_page`, `browser_screenshot`)
are `needs_permission=False` and `plan_mode_safe=True` — the same
permission tier as [`fetch_artifact`](tools.md#fetch_artifact): they don't
change anything on the page or the disk beyond writing a screenshot file,
so they stay available even while [plan mode](tui-guide.md#plan-mode) is
active.

- **`browser_read_page`** returns the page's visible text
  (`page.inner_text("body")`), prefixed with the current URL (`--- <url>
  ---`). Large output rides pcli's existing archive-on-dispatch mechanism
  automatically — the same one that archives any oversized tool result in
  general (see [Artifact archiving](tools.md#artifact-archiving)) — with
  no special-casing needed in the browser tool itself.
- **`browser_screenshot`** saves a PNG to disk under
  `browser_screenshots_dir()` (`config/paths.py`,
  `data_dir()/browser_screenshots/`), named `shot_<id>.png`
  (`pcli.util.ids.new_id`), and reports the saved file's path in its
  result text — there's no way to render an image inline in a terminal
  UI, so this is "here's where I saved it," not an inline preview.

**Every tool reports a clean, specific error instead of crashing**, in two
distinct cases:

- **No browser session configured at all** (`ctx.browser_session is
  None`) — a caller that never set one up, e.g. a bare `ToolContext` built
  directly in a test: `"Browser tools aren't available in this context
  (no browser session configured)."`
- **`BrowserSession` raises `BrowserUnavailableError`** — Playwright isn't
  actually installed, or Chromium hasn't been downloaded — see the next
  section for the exact text.

## When the extra or Chromium isn't installed

Two distinct failure points, both raising `BrowserUnavailableError`
(`src/pcli/browser/session.py`), and both caught by every `browser_*` tool
handler and turned into a clean `is_error=True` result rather than a raw
traceback:

- **Playwright itself isn't installed** (the extra was never `pip
  install`ed) — `_ensure_page`'s `import playwright.async_api` raises
  `ImportError`, re-raised as:

  ```
  Browser tools need the optional "browser" extra: run `pip install -e ".[browser]"`, then `playwright install chromium` once to download the browser itself.
  ```

- **Playwright is installed but Chromium hasn't been downloaded yet** —
  `launch_persistent_context` raises an exception whose message contains
  `"Executable doesn't exist"`, re-raised as:

  ```
  Chromium isn't downloaded yet - run `playwright install chromium` once.
  ```

In both cases, the tool's own error output appends a second, matching
suggestion on top of that message (`_INSTALL_SUGGESTION`,
`browser_tool.py`):

```
[pcli] Suggestion: install the optional browser extra and its browser binary once - `pip install -e ".[browser]"` then `playwright install chromium` - then try again.
```

So the model (and, in the TUI, the person watching) sees a specific,
actionable error either way, not a bare stack trace — consistent with how
every other builtin tool in pcli reports a failure (see
[`tools.md`](tools.md) for the pattern repeated across `run_shell`,
`download_file`, `web_fetch`, etc.).

## Headed vs. headless

`BrowserSession.__init__` takes `headless: bool` as a constructor
parameter, and the two front ends deliberately default it differently:

- **The TUI (`ChatScreen`) always launches it headed (visible)** —
  `build_agent_runtime`'s `browser_headless` parameter defaults to
  `False`. This is an explicit design goal, not an oversight: being able
  to *watch* the browser operate a real site matters for trust — e.g.
  before letting pcli run something like trading automation unattended,
  seeing it click and type on the real page first is what makes that
  trustworthy.
- **`pcli run` defaults to headless** — `run_command` (`src/pcli/cli.py`)
  passes `browser_headless=not headed`, and its new `--headed` flag
  defaults to `False`, so a plain `pcli run` gets `browser_headless=True`.
  A scheduled/unattended run has nothing to show and shouldn't pop up a
  window; pass `--headed` to override and watch it anyway. See
  [`headless-and-scheduled-runs.md`](headless-and-scheduled-runs.md#pcli-run-one-shot-task-execution)
  for `pcli run`'s other flags.

## Wiring

`AgentRuntime.browser_session` (`agent/runtime.py`) is always constructed
by `build_agent_runtime` — no conditional, since a `BrowserSession` is
cheap even when the `browser` extra isn't installed or no browser tool is
ever called in that session. It's threaded into `ToolContext` via
`make_tool_context` and `ChatScreen._make_tool_context`
(`ToolContext.browser_session`, `tools/base.py`), and registered into the
default tool registry alongside every other builtin
(`build_default_registry`, `tools/registry.py`).

`ChatScreen` also stores it on `self._browser_session` and closes it in
`on_unmount`, and `pcli run` closes `runtime.browser_session` in its
`finally` block right alongside `runtime.client.aclose()` — see
[`sandbox-and-permissions.md`](sandbox-and-permissions.md) for the same
cleanup pattern already established for the sandbox/gateway client.
