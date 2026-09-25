"""Data model for `pcli schedule`'s crontab-like recurring task scheduling -
see scheduler/store.py for persistence, scheduler/runner.py for how a job
actually executes (shared with `pcli run`), and scheduler/daemon.py for the
loop that decides when a job is due."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from pcli.util.ids import new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ScheduleJob(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job_"))
    name: str = ""
    cron: str
    """Standard 5-field cron expression (minute hour day month weekday),
    e.g. "*/15 * * * *". Validated via croniter.is_valid before a job is
    ever saved - see scheduler/store.py's add_job."""
    task: str | None = None
    task_file: str | None = None
    """Exactly one of task/task_file is set - mirrors `pcli run`'s own
    --task/--task-file mutual exclusivity."""
    session_id: str | None = None
    """Append to this one continuing session on every run, or None for a
    fresh session each time - mirrors `pcli run --session`."""
    headed: bool = False
    notify_telegram: bool = False
    quiet: bool = True
    enabled: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    next_run_at: datetime | None = None
    """Computed lazily by the daemon on first seeing a job with this unset
    (or after it fires) via croniter(cron, base=...).get_next(datetime) -
    not computed at job-creation time so a job added while the daemon isn't
    running doesn't carry a stale "next run" from whenever `schedule add`
    happened to execute."""
    last_run_at: datetime | None = None
    last_status: str | None = None
    """"ok" or "error" after the most recent run; None if it has never run."""
    last_error: str | None = None


class ScheduleStore(BaseModel):
    jobs: list[ScheduleJob] = Field(default_factory=list)
