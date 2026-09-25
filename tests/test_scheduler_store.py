"""Coverage for scheduler/store.py: on-disk persistence for pcli schedule's
job list. Isolated automatically by tests/conftest.py's autouse
_isolated_pcli_paths fixture (redirects data_dir() to a per-test tmp_path),
so no explicit path override is needed here."""

from pcli.config.paths import schedule_file
from pcli.scheduler.models import ScheduleJob
from pcli.scheduler.store import (
    add_job,
    get_job,
    read_schedule,
    remove_job,
    set_job_enabled,
    update_job,
)


def test_add_job_persists_to_disk():
    job = add_job(ScheduleJob(cron="*/15 * * * *", task="check the thing"))

    assert schedule_file().exists()
    stored = read_schedule()
    assert len(stored.jobs) == 1
    assert stored.jobs[0].id == job.id
    assert stored.jobs[0].cron == "*/15 * * * *"
    assert stored.jobs[0].task == "check the thing"


def test_read_schedule_with_no_file_yet_returns_empty_store():
    assert read_schedule().jobs == []


def test_get_job_finds_by_id():
    job = add_job(ScheduleJob(cron="* * * * *", task="x"))
    add_job(ScheduleJob(cron="* * * * *", task="y"))

    found = get_job(job.id)
    assert found is not None
    assert found.task == "x"


def test_get_job_returns_none_for_unknown_id():
    assert get_job("job_does_not_exist") is None


def test_remove_job_deletes_and_returns_true():
    job = add_job(ScheduleJob(cron="* * * * *", task="x"))

    assert remove_job(job.id) is True
    assert read_schedule().jobs == []


def test_remove_job_returns_false_for_unknown_id():
    assert remove_job("job_does_not_exist") is False


def test_set_job_enabled_toggles_and_persists():
    job = add_job(ScheduleJob(cron="* * * * *", task="x"))
    assert job.enabled is True

    assert set_job_enabled(job.id, False) is True
    assert read_schedule().jobs[0].enabled is False

    assert set_job_enabled(job.id, True) is True
    assert read_schedule().jobs[0].enabled is True


def test_set_job_enabled_returns_false_for_unknown_id():
    assert set_job_enabled("job_does_not_exist", False) is False


def test_update_job_replaces_by_id_and_preserves_other_jobs():
    job_a = add_job(ScheduleJob(cron="* * * * *", task="a"))
    job_b = add_job(ScheduleJob(cron="* * * * *", task="b"))

    job_a.last_status = "ok"
    job_a.task = "a changed"
    update_job(job_a)

    reloaded = {j.id: j for j in read_schedule().jobs}
    assert reloaded[job_a.id].task == "a changed"
    assert reloaded[job_a.id].last_status == "ok"
    assert reloaded[job_b.id].task == "b"  # untouched
