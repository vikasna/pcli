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


# --- file_change / git_commit triggers ---


def _git(repo, *args: str):
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


async def _fake_run_task_once_ok(task, **kwargs):
    session = Session(id="sess_x", model="fake-model", gateway_base_url="http://x")
    return HeadlessTurnResult(session=session, final_text="done", terminated_early=False)


@pytest.mark.asyncio
async def test_file_change_job_baselines_without_firing_on_first_poll(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    watched = tmp_path / "watched.txt"
    watched.write_text("v1", encoding="utf-8")
    add_job(ScheduleJob(trigger="file_change", watch_path=str(watched), task="x"))

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == []
    job = read_schedule().jobs[0]
    assert job.last_seen_state is not None
    assert job.last_run_at is None


@pytest.mark.asyncio
async def test_file_change_job_fires_once_the_watched_path_actually_changes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    watched = tmp_path / "watched.txt"
    watched.write_text("v1", encoding="utf-8")
    job = add_job(ScheduleJob(trigger="file_change", watch_path=str(watched), task="x"))

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)
    store = SessionStore(base_dir=tmp_path / "sessions")

    # First poll: baseline only, no fire.
    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)
    assert calls == []

    watched.write_text("v2 - actually different", encoding="utf-8")

    # Second poll: the watched file genuinely changed.
    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)
    assert calls == ["x"]

    reloaded = read_schedule().jobs[0]
    assert reloaded.id == job.id
    assert reloaded.last_status == "ok"
    assert reloaded.last_run_at is not None


@pytest.mark.asyncio
async def test_file_change_job_does_not_refire_on_unchanged_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    watched = tmp_path / "watched.txt"
    watched.write_text("v1", encoding="utf-8")
    add_job(ScheduleJob(trigger="file_change", watch_path=str(watched), task="x"))

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)
    store = SessionStore(base_dir=tmp_path / "sessions")

    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)  # baseline
    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)  # nothing changed

    assert calls == []


@pytest.mark.asyncio
async def test_file_change_job_baselines_after_the_run_not_before(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Regression guard for the self-trigger-loop fix: a task that itself
    edits the watched path must not see its own edit as "one more change"
    on the very next poll."""
    watched = tmp_path / "watched.txt"
    watched.write_text("v1", encoding="utf-8")
    add_job(ScheduleJob(trigger="file_change", watch_path=str(watched), task="x"))

    calls: list[str] = []

    async def self_editing_run_task_once(task, **kwargs):
        calls.append(task)
        watched.write_text("edited by the task itself", encoding="utf-8")
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", self_editing_run_task_once)
    store = SessionStore(base_dir=tmp_path / "sessions")

    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)  # baseline
    watched.write_text("v2 - an external change", encoding="utf-8")
    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)  # fires, self-edits
    assert calls == ["x"]

    # No further external change happened - must not fire again.
    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)
    assert calls == ["x"]


@pytest.mark.asyncio
async def test_git_commit_job_baselines_without_firing_on_first_poll(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "first commit")

    add_job(ScheduleJob(trigger="git_commit", watch_git_repo=str(repo), task="x"))

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == []
    job = read_schedule().jobs[0]
    assert job.last_seen_state is not None


@pytest.mark.asyncio
async def test_git_commit_job_fires_once_a_new_commit_lands(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "first commit")

    job = add_job(ScheduleJob(trigger="git_commit", watch_git_repo=str(repo), task="x"))

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)
    store = SessionStore(base_dir=tmp_path / "sessions")

    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)  # baseline
    assert calls == []

    (repo / "file.txt").write_text("v2", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "second commit")

    await _run_due_jobs(_settings(), store, tmp_path, on_job_run=None)
    assert calls == ["x"]

    reloaded = read_schedule().jobs[0]
    assert reloaded.id == job.id
    assert reloaded.last_status == "ok"


@pytest.mark.asyncio
async def test_git_commit_job_with_no_repo_configured_uses_cwd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """watch_git_repo unset -> falls back to the cwd _run_due_jobs was
    called with (the daemon's own cwd), same fallback documented on
    ScheduleJob.watch_git_repo."""
    repo = tmp_path / "as-cwd"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "first commit")

    add_job(ScheduleJob(trigger="git_commit", task="x"))  # watch_git_repo left unset

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)
        return await _fake_run_task_once_ok(task, **kwargs)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), repo, on_job_run=None)

    job = read_schedule().jobs[0]
    assert job.last_seen_state is not None  # resolved via the given cwd, not an error


@pytest.mark.asyncio
async def test_git_commit_job_with_a_failed_lookup_is_skipped_not_errored(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    add_job(ScheduleJob(trigger="git_commit", watch_git_repo=str(not_a_repo), task="x"))

    calls: list[str] = []

    async def fake_run_task_once(task, **kwargs):
        calls.append(task)

    monkeypatch.setattr(daemon_module, "run_task_once", fake_run_task_once)

    await _run_due_jobs(_settings(), SessionStore(base_dir=tmp_path / "sessions"), tmp_path, on_job_run=None)

    assert calls == []
    job = read_schedule().jobs[0]
    assert job.last_seen_state is None  # never successfully baselined
    assert job.last_status is None  # skipped, not marked an error


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
