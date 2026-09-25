"""On-disk persistence for `pcli schedule`'s job list (scheduler/models.py) -
mirrors memory/store.py's shape (a single JSON file under data_dir(),
atomic-written, loaded fresh on every call rather than cached) so `pcli
schedule add` from one process and `pcli schedule run`'s daemon loop in
another always see each other's latest writes without needing to share
in-memory state or restart anything."""

from __future__ import annotations

import os
from pathlib import Path

from pcli.config.paths import schedule_file
from pcli.scheduler.models import ScheduleJob, ScheduleStore


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def read_schedule() -> ScheduleStore:
    path = schedule_file()
    if not path.exists():
        return ScheduleStore()
    return ScheduleStore.model_validate_json(path.read_text(encoding="utf-8"))


def write_schedule(store: ScheduleStore) -> None:
    _atomic_write(schedule_file(), store.model_dump_json(indent=2))


def add_job(job: ScheduleJob) -> ScheduleJob:
    store = read_schedule()
    store.jobs.append(job)
    write_schedule(store)
    return job


def remove_job(job_id: str) -> bool:
    """Returns whether anything was removed."""
    store = read_schedule()
    before = len(store.jobs)
    store.jobs = [j for j in store.jobs if j.id != job_id]
    if len(store.jobs) == before:
        return False
    write_schedule(store)
    return True


def get_job(job_id: str) -> ScheduleJob | None:
    return next((j for j in read_schedule().jobs if j.id == job_id), None)


def set_job_enabled(job_id: str, enabled: bool) -> bool:
    """Returns whether a matching job was found and updated."""
    store = read_schedule()
    for job in store.jobs:
        if job.id == job_id:
            job.enabled = enabled
            write_schedule(store)
            return True
    return False


def update_job(job: ScheduleJob) -> None:
    """Replaces the stored job with the same id as `job` - used to persist
    daemon-side bookkeeping (next_run_at/last_run_at/last_status/last_error)
    after a run, without the caller needing to hand-roll the read/mutate/
    write cycle itself."""
    store = read_schedule()
    store.jobs = [job if j.id == job.id else j for j in store.jobs]
    write_schedule(store)
