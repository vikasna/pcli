"""Coverage for scheduler/daemon.py: due-job computation (compute_next_run,
_run_due_jobs) and the top-level run_scheduler_daemon loop. run_task_once
itself (the actual task execution) is faked throughout - its own coverage
lives in test_scheduler_runner.py / test_cli_run.py."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pcli.agent.headless import HeadlessTurnResult
from pcli.config.settings import Settings
from pcli.scheduler import daemon as daemon_module
from pcli.scheduler.daemon import _run_due_jobs, compute_next_run, run_scheduler_daemon
from pcli.scheduler.models import ScheduleJob
from pcli.scheduler.runner import ScheduledSessionNotFoundError
from pcli.scheduler.store import add_job, read_schedule
from pcli.session.models import Session
from pcli.session.store import SessionStore


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
    )


def test_compute_next_run_matches_the_cron_expression():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert compute_next_run("*/15 * * * *", base=base) == datetime(2026, 1, 1, 12, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_run_due_jobs_computes_next_run_for_a_job_that_has_never_run(tmp_path):
    add_job(ScheduleJob(cron="*/15 * * * *", task="x"))

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    job = read_schedule().jobs[0]
    assert job.next_run_at is not None
    assert job.last_run_at is None  # first tick only computes next_run_at, doesn't fire yet


@pytest.mark.asyncio
async def test_run_due_jobs_fires_a_due_job(tmp_path, monkeypatch: pytest.MonkeyPatch):
    job = add_job(
        ScheduleJob(
            cron="* * * * *",
            task="do the thing",
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),  # already due
        )
    )

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        session = Session(id="sess_x", model="fake-model", gateway_base_url="http://x")
        return HeadlessTurnResult(session=session, final_text="done", terminated_early=False)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == ["do the thing"]
    reloaded = read_schedule().jobs[0]
    assert reloaded.id == job.id
    assert reloaded.last_status == "ok"
    assert reloaded.last_run_at is not None
    assert reloaded.next_run_at > datetime.now(UTC)  # advanced past "now"


@pytest.mark.asyncio
async def test_run_due_jobs_applies_a_per_job_max_cost_override(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """job.max_cost_usd overrides max_session_cost_usd for just this job's
    own run (via settings.model_copy in _run_one_job), without mutating the
    shared Settings object other jobs/the daemon loop itself use."""
    add_job(
        ScheduleJob(
            cron="* * * * *",
            task="x",
            max_cost_usd=2.50,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    seen_max_costs: list[float | None] = []

    async def fake_run_task_once(task, *, settings, **kwargs):
        seen_max_costs.append(settings.max_session_cost_usd)
        session = Session(id="sess_x", model="fake-model", gateway_base_url="http://x")
        return HeadlessTurnResult(session=session, final_text="done", terminated_early=False)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    shared_settings = _settings()
    assert shared_settings.max_session_cost_usd is None
    await _run_due_jobs(shared_settings, SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert seen_max_costs == [2.50]
    assert shared_settings.max_session_cost_usd is None  # unmutated


@pytest.mark.asyncio
async def test_run_due_jobs_without_a_per_job_max_cost_uses_the_shared_settings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    add_job(
        ScheduleJob(cron="* * * * *", task="x", next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    seen_max_costs: list[float | None] = []

    async def fake_run_task_once(task, *, settings, **kwargs):
        seen_max_costs.append(settings.max_session_cost_usd)
        session = Session(id="sess_x", model="fake-model", gateway_base_url="http://x")
        return HeadlessTurnResult(session=session, final_text="done", terminated_early=False)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    settings_with_cap = _settings().model_copy(update={"max_session_cost_usd": 5.0})
    await _run_due_jobs(settings_with_cap, SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert seen_max_costs == [5.0]


@pytest.mark.asyncio
async def test_run_due_jobs_skips_a_not_due_job(tmp_path, monkeypatch: pytest.MonkeyPatch):
    add_job(
        ScheduleJob(
            cron="* * * * *",
            task="x",
            next_run_at=datetime.now(UTC) + timedelta(hours=1),  # not due yet
        )
    )
    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        raise AssertionError("should not have been called")

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == []


@pytest.mark.asyncio
async def test_run_due_jobs_skips_a_disabled_job(tmp_path, monkeypatch: pytest.MonkeyPatch):
    add_job(
        ScheduleJob(
            cron="* * * * *",
            task="x",
            enabled=False,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == []


@pytest.mark.asyncio
async def test_run_due_jobs_records_a_failure_without_crashing(tmp_path, monkeypatch: pytest.MonkeyPatch):
    job = add_job(
        ScheduleJob(cron="* * * * *", task="x", next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    )

    async def failing_run_task_once(task, **kwargs):
        raise RuntimeError("gateway is down")

    monkeypatch.setattr(daemon_module, "run_task_once", failing_run_task_once)

    # Must not raise - one job failing can't kill the daemon loop.
    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    reloaded = read_schedule().jobs[0]
    assert reloaded.id == job.id
    assert reloaded.last_status == "error"
    assert "gateway is down" in reloaded.last_error
    assert reloaded.next_run_at is not None  # still advanced, so it retries next cycle


@pytest.mark.asyncio
async def test_run_due_jobs_records_a_missing_session_as_an_error(tmp_path, monkeypatch: pytest.MonkeyPatch):
    add_job(
        ScheduleJob(
            cron="* * * * *",
            task="x",
            session_id="sess_gone",
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )

    async def missing_session_run_task_once(task, **kwargs):
        raise ScheduledSessionNotFoundError("sess_gone")

    monkeypatch.setattr(daemon_module, "run_task_once", missing_session_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    reloaded = read_schedule().jobs[0]
    assert reloaded.last_status == "error"
    assert "sess_gone" in reloaded.last_error


@pytest.mark.asyncio
async def test_run_due_jobs_reads_the_task_from_task_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    task_file = tmp_path / "task.md"
    task_file.write_text("step 1\nstep 2", encoding="utf-8")
    add_job(
        ScheduleJob(
            cron="* * * * *",
            task_file=str(task_file),
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        session = Session(id="sess_x", model="fake-model", gateway_base_url="http://x")
        return HeadlessTurnResult(session=session, final_text="done", terminated_early=False)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == ["step 1\nstep 2"]


@pytest.mark.asyncio
async def test_run_due_jobs_calls_on_job_run_callback(tmp_path, monkeypatch: pytest.MonkeyPatch):
    add_job(ScheduleJob(cron="* * * * *", task="x", next_run_at=datetime.now(UTC) - timedelta(minutes=1)))

    async def fake_run_task_once(task, **kwargs):
        session = Session(id="sess_x", model="fake-model", gateway_base_url="http://x")
        return HeadlessTurnResult(session=session, final_text="done", terminated_early=False)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    seen = []
    await _run_due_jobs(
        _settings(),
        SessionStore(base_dir=tmp_path / "sessions"),
        tmp_path,
        on_job_run=lambda job, result, error: seen.append((job.task, result, error)),
    )

    assert len(seen) == 1
    task, result, error = seen[0]
    assert task == "x"
    assert result is not None and result.final_text == "done"
    assert error is None


@pytest.mark.asyncio
async def test_run_scheduler_daemon_ticks_and_can_be_cancelled(monkeypatch: pytest.MonkeyPatch):
    """The top-level loop itself: sleep is stubbed to return instantly so
    the test doesn't actually wait poll_interval_s between ticks."""
    tick_count = 0
    real_sleep = asyncio.sleep

    async def instant_sleep(_seconds: float) -> None:
        # Still a real (zero-second) sleep, not a no-op coroutine - an
        # `async def` with no internal await point completes synchronously
        # when awaited, without ever yielding to the event loop, which
        # would starve the test's own polling loop below of any chance to
        # interleave with the daemon task.
        await real_sleep(0)

    monkeypatch.setattr(daemon_module.asyncio, "sleep", instant_sleep)

    def on_tick() -> None:
        nonlocal tick_count
        tick_count += 1

    task = asyncio.create_task(
        run_scheduler_daemon(_settings(), poll_interval_s=30, on_tick=on_tick)
    )
    for _ in range(50):
        if tick_count >= 3:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tick_count >= 3
