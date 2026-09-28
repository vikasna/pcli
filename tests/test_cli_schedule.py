"""Coverage for `pcli schedule` (add/list/remove/enable/disable/run-now) -
scheduler/store.py and scheduler/daemon.py have their own dedicated unit
test files; this covers the CLI command's own argument handling and error
paths. `pcli schedule run` (the long-running daemon itself) isn't invoked
here - Ctrl+C-only, tested via run_scheduler_daemon directly in
test_scheduler_daemon.py instead."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pcli.cli import app
from pcli.config import settings as settings_module
from pcli.scheduler.models import ScheduleJob
from pcli.scheduler.store import add_job, read_schedule

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "_settings", None)


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("pcli.config.paths.data_dir", lambda: data_dir)
    return data_dir


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _final_response(text: str) -> httpx.Response:
    return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}))


def test_schedule_add_requires_exactly_one_of_task_or_task_file(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "add", "--cron", "* * * * *"])
    assert result.exit_code == 1
    assert "exactly one of --task or --task-file" in result.output


def test_schedule_add_rejects_an_invalid_cron_expression(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "add", "--cron", "not a cron", "--task", "x"])
    assert result.exit_code == 1
    assert "valid 5-field cron expression" in result.output
    assert read_schedule().jobs == []


def test_schedule_add_saves_a_job(isolated_paths: Path):
    result = runner.invoke(
        app, ["schedule", "add", "--cron", "*/15 * * * *", "--task", "check the site", "--name", "check"]
    )
    assert result.exit_code == 0
    assert "Added job" in result.output

    jobs = read_schedule().jobs
    assert len(jobs) == 1
    assert jobs[0].cron == "*/15 * * * *"
    assert jobs[0].task == "check the site"
    assert jobs[0].name == "check"
    assert jobs[0].enabled is True


def test_schedule_add_task_file(isolated_paths: Path, tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("do the multi-step thing", encoding="utf-8")

    result = runner.invoke(app, ["schedule", "add", "--cron", "0 9 * * *", "--task-file", str(task_file)])

    assert result.exit_code == 0
    jobs = read_schedule().jobs
    assert jobs[0].task_file == str(task_file)


def test_schedule_list_with_no_jobs(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0
    assert "No scheduled jobs" in result.output


def test_schedule_list_shows_added_jobs(isolated_paths: Path):
    add_job(ScheduleJob(cron="* * * * *", task="x", name="my job"))

    result = runner.invoke(app, ["schedule", "list"])

    assert result.exit_code == 0
    assert "my job" in result.output
    assert "* * * * *" in result.output
    assert "enabled" in result.output


def test_schedule_remove_deletes_a_job(isolated_paths: Path):
    job = add_job(ScheduleJob(cron="* * * * *", task="x"))

    result = runner.invoke(app, ["schedule", "remove", job.id])

    assert result.exit_code == 0
    assert read_schedule().jobs == []


def test_schedule_remove_unknown_id_errors_cleanly(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "remove", "job_does_not_exist"])
    assert result.exit_code == 1
    assert "No job found" in result.output


def test_schedule_disable_then_enable(isolated_paths: Path):
    job = add_job(ScheduleJob(cron="* * * * *", task="x"))

    result = runner.invoke(app, ["schedule", "disable", job.id])
    assert result.exit_code == 0
    assert read_schedule().jobs[0].enabled is False

    result = runner.invoke(app, ["schedule", "enable", job.id])
    assert result.exit_code == 0
    assert read_schedule().jobs[0].enabled is True


def test_schedule_enable_unknown_id_errors_cleanly(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "enable", "job_does_not_exist"])
    assert result.exit_code == 1
    assert "No job found" in result.output


@respx.mock
def test_schedule_run_now_fires_the_job_immediately(isolated_paths: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_final_response("the answer")
    )
    job = add_job(ScheduleJob(cron="0 0 1 1 *", task="say hello"))  # next real run: a year away

    result = runner.invoke(
        app,
        [
            "--gateway-url", "http://fake-gateway.test/v1",
            "--api-key", "test-key",
            "--model", "fake-model",
            "schedule", "run-now", job.id,
        ],
    )

    assert result.exit_code == 0
    assert "the answer" in result.output


def test_schedule_run_now_unknown_id_errors_cleanly(isolated_paths: Path):
    result = runner.invoke(
        app,
        [
            "--gateway-url", "http://fake-gateway.test/v1",
            "--api-key", "test-key",
            "--model", "fake-model",
            "schedule", "run-now", "job_does_not_exist",
        ],
    )
    assert result.exit_code == 1
    assert "No job found" in result.output


def test_schedule_run_now_requires_a_configured_gateway(isolated_paths: Path):
    job = add_job(ScheduleJob(cron="* * * * *", task="x"))
    result = runner.invoke(app, ["schedule", "run-now", job.id])
    assert result.exit_code == 1
    assert "Gateway not configured" in result.output


def test_schedule_run_requires_a_configured_gateway(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "run"])
    assert result.exit_code == 1
    assert "Gateway not configured" in result.output


def test_schedule_add_saves_a_per_job_max_cost(isolated_paths: Path):
    """--max-cost is per-job (ScheduleJob.max_cost_usd), overriding
    max_session_cost_usd for just this job's own runs - see
    scheduler/daemon.py's _run_one_job for how it's applied."""
    result = runner.invoke(
        app,
        ["schedule", "add", "--cron", "* * * * *", "--task", "x", "--max-cost", "2.50"],
    )
    assert result.exit_code == 0

    jobs = read_schedule().jobs
    assert jobs[0].max_cost_usd == 2.50


def test_schedule_add_without_max_cost_leaves_it_unset(isolated_paths: Path):
    result = runner.invoke(app, ["schedule", "add", "--cron", "* * * * *", "--task", "x"])
    assert result.exit_code == 0

    jobs = read_schedule().jobs
    assert jobs[0].max_cost_usd is None
