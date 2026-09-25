"""Executes one task end-to-end against the headless runtime - the shared
implementation behind both `pcli run` (cli.py) and a scheduled job firing
(scheduler/daemon.py). Factored out so the two entry points can't drift:
both build the same AgentRuntime, load/create the session the same way, run
the same run_headless_task, and send the same optional Telegram
notification.
"""

from __future__ import annotations

from pathlib import Path

from pcli.agent.headless import (
    HeadlessTurnResult,
    ProgressCallback,
    new_headless_session,
    run_headless_task,
)
from pcli.agent.runtime import build_agent_runtime, build_permission_manager
from pcli.config.settings import Settings
from pcli.session.store import SessionNotFoundError, SessionStore
from pcli.telegram.bot import notify_telegram


class ScheduledSessionNotFoundError(Exception):
    """Raised when a job's session_id no longer resolves to a real session
    (e.g. it was deleted via `pcli sessions`) - the caller decides whether
    that's fatal for this run or should fall back to a fresh session."""


async def run_task_once(
    task: str,
    *,
    settings: Settings,
    store: SessionStore,
    cwd: Path,
    session_id: str | None = None,
    quiet: bool = True,
    headed: bool = False,
    notify_telegram_flag: bool = False,
    on_progress: ProgressCallback = lambda _line: None,
) -> HeadlessTurnResult:
    """Builds a fresh AgentRuntime, loads/creates the session, runs the
    task, optionally notifies Telegram, and cleanly closes the runtime -
    the same sequence `pcli run` has always done, now also used by a fired
    scheduled job. Raises ScheduledSessionNotFoundError if session_id is
    given but doesn't resolve (the runtime is still cleaned up first)."""
    if session_id:
        try:
            session = store.load(session_id)
        except SessionNotFoundError:
            raise ScheduledSessionNotFoundError(session_id) from None
    else:
        session = new_headless_session(store, settings, cwd)

    runtime = await build_agent_runtime(settings, cwd, browser_headless=not headed)
    try:
        result = await run_headless_task(
            task,
            session=session,
            runtime=runtime,
            settings=settings,
            permission_manager=build_permission_manager(settings),
            cwd=cwd,
            store=store,
            on_progress=on_progress,
        )
    finally:
        await runtime.client.aclose()
        await runtime.browser_session.close()

    if notify_telegram_flag:
        if not settings.is_telegram_configured():
            on_progress(
                "notify_telegram was requested but telegram_bot_token/telegram_chat_id aren't "
                "configured - skipping the notification."
            )
        else:
            try:
                await notify_telegram(settings, result.final_text)
            except Exception as exc:  # noqa: BLE001 - a failed notification shouldn't fail the run
                on_progress(f"Failed to send the Telegram notification: {exc}")

    return result
