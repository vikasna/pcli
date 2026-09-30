"""The long-running loop behind `pcli schedule run` - the third kind of
daemon alongside `pcli telegram`'s (telegram/daemon.py) and the browser
session's own lazy lifecycle. Ticks on a plain poll interval (no OS-level
cron integration, no extra process) - see scheduler/models.py's ScheduleJob
for what a job stores and scheduler/runner.py's run_task_once for how one
actually executes, shared with `pcli run`.

croniter is imported lazily inside the two functions that need it, not at
this module's top level, so `import pcli.scheduler.daemon` itself doesn't
require the `schedule` extra to be installed - only actually computing a
next-run time does (mirrors browser/session.py's lazy playwright import and
telegram/sender.py's lazy python-telegram-bot import)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pcli.agent.headless import HeadlessTurnResult
from pcli.config.settings import Settings
from pcli.scheduler.models import ScheduleJob
from pcli.scheduler.runner import ScheduledSessionNotFoundError, run_task_once
from pcli.scheduler.store import read_schedule, update_job
from pcli.scheduler.triggers import compute_path_signature, get_git_head_sha
from pcli.session.store import SessionStore

logger = logging.getLogger(__name__)

OnTickCallback = Callable[[], None]
OnJobRunCallback = Callable[[ScheduleJob, "HeadlessTurnResult | None", Exception | None], None]


def compute_next_run(cron: str, *, base: datetime) -> datetime:
    from croniter import croniter

    return croniter(cron, base).get_next(datetime)


async def run_scheduler_daemon(
    settings: Settings,
    *,
    poll_interval_s: int = 30,
    on_tick: OnTickCallback | None = None,
    on_job_run: OnJobRunCallback | None = None,
) -> None:
    """Runs until cancelled (Ctrl+C from `pcli schedule run`, or the caller
    cancelling the task). Each tick re-reads schedule.json fresh (see
    scheduler/store.py's read_schedule), so `pcli schedule add`/`remove`/
    `enable`/`disable` run from another process take effect on the very
    next tick without restarting this daemon. Jobs run sequentially within
    a tick - simple, predictable, and avoids overlapping runs of possibly-
    related scheduled tasks competing for the same sandbox/session."""
    store = SessionStore()
    cwd = Path.cwd()
    while True:
        await _run_due_jobs(settings, store, cwd, on_job_run=on_job_run)
        if on_tick is not None:
            on_tick()
        await asyncio.sleep(poll_interval_s)


def _cron_due(job: ScheduleJob, now: datetime) -> bool:
    if job.next_run_at is None:
        assert job.cron is not None  # enforced by ScheduleJob's validator
        job.next_run_at = compute_next_run(job.cron, base=now)
        update_job(job)
        return False
    return job.next_run_at <= now


def _current_event_signature(job: ScheduleJob, cwd: Path) -> str | None:
    if job.trigger == "file_change":
        assert job.watch_path is not None  # enforced by ScheduleJob's validator
        return compute_path_signature(Path(job.watch_path))
    assert job.trigger == "git_commit"
    repo = Path(job.watch_git_repo) if job.watch_git_repo else cwd
    return get_git_head_sha(repo, job.watch_git_branch)


def _event_due(job: ScheduleJob, cwd: Path) -> bool:
    signature = _current_event_signature(job, cwd)
    if signature is None:
        # A git lookup failed this poll (repo not there yet, git missing,
        # ...) - already logged in triggers.py. Just skip and retry next
        # tick rather than marking the job an error.
        return False
    if job.last_seen_state is None:
        # First time this job's been seen by any poll - baseline without
        # firing, the same "compute and skip" first-tick behavior cron
        # jobs get above, so adding a job against an already-existing
        # file/commit doesn't fire immediately.
        job.last_seen_state = signature
        update_job(job)
        return False
    return signature != job.last_seen_state


def _rebaseline_event_job(job: ScheduleJob, cwd: Path) -> None:
    """Re-reads the current signature *after* the job's own run finished
    and stores that as the new baseline - deliberately not the signature
    that triggered the run. Otherwise a task that itself edits the watched
    path, or commits to the watched branch (a very plausible case - e.g. a
    task that auto-commits its own changes), would see its own output as
    "one more change" on the very next poll and fire again indefinitely.
    Baselining after the run means only changes introduced *after* the
    job's own run completed count as the next trigger."""
    signature = _current_event_signature(job, cwd)
    if signature is not None:
        job.last_seen_state = signature
        update_job(job)


async def _run_due_jobs(
    settings: Settings,
    store: SessionStore,
    cwd: Path,
    *,
    on_job_run: OnJobRunCallback | None,
) -> None:
    now = datetime.now(UTC)
    for job in read_schedule().jobs:
        if not job.enabled:
            continue
        is_due = _cron_due(job, now) if job.trigger == "cron" else _event_due(job, cwd)
        if not is_due:
            continue
        await _run_one_job(job, settings, store, cwd, on_job_run=on_job_run)
        if job.trigger != "cron":
            _rebaseline_event_job(job, cwd)


async def _run_one_job(
    job: ScheduleJob,
    settings: Settings,
    store: SessionStore,
    cwd: Path,
    *,
    on_job_run: OnJobRunCallback | None,
) -> None:
    task = job.task
    if job.task_file:
        task = Path(job.task_file).read_text(encoding="utf-8")
    assert task is not None  # ScheduleJob guarantees exactly one of task/task_file is set

    job_settings = settings
    if job.max_cost_usd is not None:
        # A local override for just this job's own runs, same reasoning as
        # `pcli run --max-cost` (cli.py's run_command) - never mutates the
        # shared settings object other jobs/the daemon loop itself use.
        job_settings = settings.model_copy(update={"max_session_cost_usd": job.max_cost_usd})

    result: HeadlessTurnResult | None = None
    error: Exception | None = None
    try:
        result = await run_task_once(
            task,
            settings=job_settings,
            store=store,
            cwd=cwd,
            session_id=job.session_id,
            quiet=job.quiet,
            headed=job.headed,
            notify_telegram_flag=job.notify_telegram,
        )
        job.last_status = "ok"
        job.last_error = None
    except ScheduledSessionNotFoundError as exc:
        error = exc
        job.last_status = "error"
        job.last_error = f"session '{job.session_id}' no longer exists"
        logger.error(
            "Scheduled job %s (%s): %s", job.id, job.name or job.cron or job.trigger, job.last_error
        )
    except Exception as exc:  # one job failing must not kill the daemon loop
        error = exc
        job.last_status = "error"
        job.last_error = str(exc)
        logger.exception(
            "Scheduled job %s (%s) failed", job.id, job.name or job.cron or job.trigger
        )

    job.last_run_at = datetime.now(UTC)
    if job.trigger == "cron":
        assert job.cron is not None  # enforced by ScheduleJob's validator
        job.next_run_at = compute_next_run(job.cron, base=job.last_run_at)
    update_job(job)

    if on_job_run is not None:
        on_job_run(job, result, error)
