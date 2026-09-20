"""Coverage for `pcli run`: one-shot, non-interactive task execution (see
agent/headless.py for the turn-driving logic itself, agent/runtime.py for
the shared startup wiring - both have their own dedicated test files;
this covers the CLI command's own argument handling, error paths, and
exit-code wiring)."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pcli.cli import app
from pcli.config import settings as settings_module
from pcli.session.models import Message
from pcli.session.store import SessionStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_settings() caches globally when called with no overrides - without
    # resetting this between tests, an earlier test's gateway_base_url leaks
    # into a later one (same fixture as test_cli_config_persist.py).
    monkeypatch.setattr(settings_module, "_settings", None)


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("pcli.config.paths.data_dir", lambda: data_dir)
    return SessionStore(base_dir=data_dir / "sessions")


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _final_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(
            {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
        ),
    )


def _base_args(*extra: str) -> list[str]:
    return [
        "--gateway-url",
        "http://fake-gateway.test/v1",
        "--api-key",
        "test-key",
        "--model",
        "fake-model",
        "run",
        *extra,
    ]


def test_run_requires_exactly_one_of_task_or_task_file(isolated_store: SessionStore):
    result = runner.invoke(app, _base_args())
    assert result.exit_code == 1
    assert "exactly one of --task or --task-file" in result.output


def test_run_rejects_both_task_and_task_file(isolated_store: SessionStore, tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("do it", encoding="utf-8")
    result = runner.invoke(app, _base_args("--task", "inline", "--task-file", str(task_file)))
    assert result.exit_code == 1
    assert "exactly one of --task or --task-file" in result.output


def test_run_requires_a_configured_gateway(isolated_store: SessionStore):
    result = runner.invoke(app, ["run", "--task", "say hi"])
    assert result.exit_code == 1
    assert "Gateway not configured" in result.output


@respx.mock
def test_run_happy_path_prints_the_answer_and_saves_the_session(isolated_store: SessionStore):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_final_response("All done, task complete.")
    )

    result = runner.invoke(app, _base_args("--task", "say hello"))

    assert result.exit_code == 0
    assert "All done, task complete." in result.output
    assert "pcli --resume" in result.output

    entries = isolated_store.list_index()
    assert len(entries) == 1
    session = isolated_store.load(entries[0].id)
    assert any(m.role == "user" and m.content == "say hello" for m in session.messages)
    assert any(m.content == "All done, task complete." for m in session.messages)


@respx.mock
def test_run_task_file_reads_the_task_from_disk(isolated_store: SessionStore, tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_final_response("done")
    )
    task_file = tmp_path / "task.md"
    task_file.write_text("multi-line\ntask instructions", encoding="utf-8")

    result = runner.invoke(app, _base_args("--task-file", str(task_file)))

    assert result.exit_code == 0
    entries = isolated_store.list_index()
    session = isolated_store.load(entries[0].id)
    assert any(m.role == "user" and m.content == "multi-line\ntask instructions" for m in session.messages)


@respx.mock
def test_run_quiet_suppresses_progress_but_shows_the_final_answer(isolated_store: SessionStore):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_final_response("the answer")
    )

    result = runner.invoke(app, _base_args("--task", "say hello", "--quiet"))

    assert result.exit_code == 0
    assert "> say hello" not in result.output  # the echoed task line is progress, suppressed
    assert "the answer" in result.output


@respx.mock
def test_run_session_resumes_and_appends_to_an_existing_session(isolated_store: SessionStore):
    existing = isolated_store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    existing.messages.append(Message(role="system", content="system prompt"))
    existing.messages.append(Message(role="user", content="earlier task"))
    existing.messages.append(Message(role="assistant", content="earlier answer"))
    isolated_store.save(existing)

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_final_response("second answer")
    )

    result = runner.invoke(app, _base_args("--task", "second task", "--session", existing.id))

    assert result.exit_code == 0
    assert existing.id in result.output
    reloaded = isolated_store.load(existing.id)
    contents = [m.content for m in reloaded.messages]
    assert "earlier task" in contents  # original history preserved
    assert "second task" in contents  # new task appended, not a fresh session
    assert "second answer" in contents
    # No second session was created.
    assert len(isolated_store.list_index()) == 1


def test_run_unknown_session_id_errors_cleanly(isolated_store: SessionStore):
    result = runner.invoke(app, _base_args("--task", "x", "--session", "sess_does_not_exist"))
    assert result.exit_code == 1
    assert "No session found with id 'sess_does_not_exist'" in result.output


def test_run_exits_nonzero_when_the_turn_terminated_early(
    isolated_store: SessionStore, monkeypatch: pytest.MonkeyPatch
):
    """Isolated from agent_loop's own iteration-cap behavior (covered by
    test_agent_loop.py) - this only checks that run_command's exit-code
    wiring actually reacts to HeadlessTurnResult.terminated_early, by
    faking out build_agent_runtime/run_headless_task entirely."""
    from unittest.mock import AsyncMock

    from pcli.agent.headless import HeadlessTurnResult
    from pcli.agent.runtime import AgentRuntime

    fake_runtime = AgentRuntime(
        sandbox=None,
        tool_registry=None,
        toolbox_manager=None,
        toolbox_tools_loaded=0,
        agent_tools_loaded=0,
        client=AsyncMock(),
    )

    async def _fake_run_headless_task(task, *, session, **kwargs):
        return HeadlessTurnResult(session=session, final_text="incomplete", terminated_early=True)

    monkeypatch.setattr("pcli.cli.build_agent_runtime", AsyncMock(return_value=fake_runtime))
    monkeypatch.setattr("pcli.cli.run_headless_task", _fake_run_headless_task)

    result = runner.invoke(app, _root_and_run_args())
    assert result.exit_code == 1


def _root_and_run_args() -> list[str]:
    return [
        "--gateway-url",
        "http://fake-gateway.test/v1",
        "--api-key",
        "test-key",
        "--model",
        "fake-model",
        "run",
        "--task",
        "x",
    ]
