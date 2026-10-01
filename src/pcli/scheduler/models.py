"""Data model for `pcli schedule`'s crontab-like recurring task scheduling -
see scheduler/store.py for persistence, scheduler/runner.py for how a job
actually executes (shared with `pcli run`), and scheduler/daemon.py for the
loop that decides when a job is due."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from pcli.util.ids import new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ScheduleJob(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job_"))
    name: str = ""
    trigger: Literal["cron", "file_change", "git_commit"] = "cron"
    """Defaults to "cron" so every job already persisted in an existing
    schedule.json (written before this field existed) still validates and
    behaves exactly as before - see the model_validator below for what
    each trigger kind requires."""
    cron: str | None = None
    """Standard 5-field cron expression (minute hour day month weekday),
    e.g. "*/15 * * * *". Required (and validated via croniter.is_valid
    before a job is ever saved - see scheduler/store.py's add_job) when
    trigger == "cron"; otherwise unused."""
    watch_path: str | None = None
    """trigger == "file_change" only: a file or directory (watched
    recursively) whose contents/mtimes are polled for changes - see
    scheduler/triggers.py's compute_path_signature."""
    watch_git_repo: str | None = None
    """trigger == "git_commit" only: path to the repo to watch, defaulting
    to the daemon's own cwd if unset."""
    watch_git_branch: str | None = None
    """trigger == "git_commit" only: branch to watch, defaulting to
    whatever's currently checked out (bare HEAD) if unset."""
    last_seen_state: str | None = None
    """trigger in ("file_change", "git_commit") only: the signature/commit
    SHA last observed by the daemon (scheduler/triggers.py), used to
    detect a change on the next poll. None means "never polled yet" - the
    daemon baselines it on first sight without firing, the same way a
    cron job's unset next_run_at is computed-then-skipped on first sight
    rather than firing immediately."""
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
    max_cost_usd: float | None = None
    """Per-job hard spend cap (USD), overriding Settings.max_session_cost_usd
    for just this job's own runs - mirrors `pcli run --max-cost`, applied the
    same way (scheduler/daemon.py's _run_one_job): never persisted to
    config.toml, never affects other jobs or the TUI. None (the default)
    means this job falls back to whatever max_session_cost_usd is already
    configured (unset by default - no cap)."""
    audit: bool = False
    """Per-job override of Settings.audit_mode_enabled, applied the same
    way as max_cost_usd above - mirrors `pcli run --audit`. False (the
    default) means this job falls back to whatever audit_mode_enabled is
    already configured (off by default)."""
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

    @model_validator(mode="after")
    def _validate_trigger_fields(self) -> ScheduleJob:
        if self.trigger == "cron" and not self.cron:
            raise ValueError("trigger 'cron' requires cron to be set.")
        if self.trigger == "file_change" and not self.watch_path:
            raise ValueError("trigger 'file_change' requires watch_path to be set.")
        # "git_commit" has no required field: watch_git_repo/watch_git_branch
        # both default sensibly (daemon's cwd / whatever's checked out).
        return self


class ScheduleStore(BaseModel):
    jobs: list[ScheduleJob] = Field(default_factory=list)
