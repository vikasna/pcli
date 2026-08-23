from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.models import Message, Session
from pcli.session.store import SessionStore
from pcli.tools.base import ToolContext
from pcli.tools.builtin.todo_tool import WRITE_TODOS, render_todos


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("write_todos should never touch the sandbox")


def _ctx(tmp_path: Path, session: Session | None) -> ToolContext:
    return ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, session=session)


@pytest.mark.asyncio
async def test_write_todos_without_session_reports_error(tmp_path: Path):
    result = await WRITE_TODOS.handler({"todos": []}, _ctx(tmp_path, None))
    assert result.is_error is True
    assert "No session" in result.output


@pytest.mark.asyncio
async def test_write_todos_sets_session_todos(tmp_path: Path):
    session = Session()
    result = await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "Write tests", "status": "in_progress"},
                {"content": "Ship it", "status": "pending"},
            ]
        },
        _ctx(tmp_path, session),
    )
    assert result.is_error is False
    assert len(session.todos) == 2
    assert session.todos[0].content == "Write tests"
    assert session.todos[0].status == "in_progress"
    assert "[~] Write tests" in result.output
    assert "[ ] Ship it" in result.output


@pytest.mark.asyncio
async def test_write_todos_replaces_rather_than_appends(tmp_path: Path):
    session = Session()
    await WRITE_TODOS.handler({"todos": [{"content": "A", "status": "pending"}]}, _ctx(tmp_path, session))
    await WRITE_TODOS.handler({"todos": [{"content": "B", "status": "pending"}]}, _ctx(tmp_path, session))
    assert [t.content for t in session.todos] == ["B"]


@pytest.mark.asyncio
async def test_write_todos_rejects_multiple_in_progress(tmp_path: Path):
    session = Session()
    result = await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "A", "status": "in_progress"},
                {"content": "B", "status": "in_progress"},
            ]
        },
        _ctx(tmp_path, session),
    )
    assert result.is_error is True
    assert "one todo" in result.output
    assert session.todos == []  # rejected update must not mutate session state


@pytest.mark.asyncio
async def test_write_todos_rejects_non_list():
    session = Session()
    result = await WRITE_TODOS.handler({"todos": "not a list"}, _ctx(Path("."), session))
    assert result.is_error is True


@pytest.mark.asyncio
async def test_write_todos_rejects_malformed_entry(tmp_path: Path):
    session = Session()
    result = await WRITE_TODOS.handler(
        {"todos": [{"status": "pending"}]}, _ctx(tmp_path, session)  # missing "content"
    )
    assert result.is_error is True


def test_render_todos_empty():
    assert render_todos([]) == "Todo list is empty."


def test_todos_survive_session_export_import(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    session.messages.append(Message(role="user", content="hi"))

    import asyncio

    asyncio.run(
        WRITE_TODOS.handler(
            {"todos": [{"content": "Do the thing", "status": "in_progress"}]},
            ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, session=session),
        )
    )
    store.save(session)

    out_path = tmp_path / "export.pcli-session.json"
    export_session(session, out_path, store=store)
    imported = import_session(out_path, store=SessionStore(base_dir=tmp_path / "sessions2"))

    assert len(imported.todos) == 1
    assert imported.todos[0].content == "Do the thing"
    assert imported.todos[0].status == "in_progress"


# --- Dropped-completed-item detection ---
#
# Regression coverage for a real debugged case: a model silently replaced a
# todo list that had completed items (real, already-verified work) with a
# superficially similar but different task list, with no explanation, and
# proceeded to redo the already-finished work. write_todos still applies the
# update (a genuine restart is sometimes correct) but now surfaces the loss.


@pytest.mark.asyncio
async def test_write_todos_flags_when_completed_items_vanish(tmp_path: Path):
    session = Session()
    await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "Load California Housing dataset and save to CSV.", "status": "completed"},
                {"content": "Preprocess the dataset (split, scale, encode).", "status": "completed"},
                {"content": "Implement Baseline Model (Linear Regression) from scratch.", "status": "completed"},
                {"content": "Implement MLP from scratch.", "status": "completed"},
                {"content": "Train models and collect metrics.", "status": "in_progress"},
                {"content": "Generate plots and final HTML report.", "status": "pending"},
            ]
        },
        _ctx(tmp_path, session),
    )

    # The actual session's regression shape: 4 completed items replaced by 3
    # brand-new, unrelated pending items.
    result = await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "Download California Housing dataset and prepare data.", "status": "pending"},
                {"content": "Implement train.py to execute the training of both models.", "status": "pending"},
                {"content": "Run training and generate report.", "status": "pending"},
            ]
        },
        _ctx(tmp_path, session),
    )

    assert result.is_error is False  # a nudge, not a block - the update still applies
    assert len(session.todos) == 3  # new list still took effect
    # 3 of the 4 completed items have no reasonably-similar counterpart in
    # the new list (the 4th, "Load California Housing dataset and save to
    # CSV.", is textually close enough to "Download California Housing
    # dataset and prepare data." - ratio ~0.81 - to plausibly be the same
    # task reworded, so the fuzzy matcher correctly doesn't flag it).
    assert "3 previously completed item(s)" in result.output
    assert "Preprocess the dataset (split, scale, encode)." in result.output
    assert "record_decision" in result.output


@pytest.mark.asyncio
async def test_write_todos_does_not_flag_normal_progress(tmp_path: Path):
    session = Session()
    await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "Load the dataset.", "status": "completed"},
                {"content": "Train the model.", "status": "in_progress"},
            ]
        },
        _ctx(tmp_path, session),
    )

    result = await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "Load the dataset.", "status": "completed"},
                {"content": "Train the model.", "status": "completed"},
                {"content": "Write the report.", "status": "in_progress"},
            ]
        },
        _ctx(tmp_path, session),
    )

    assert result.is_error is False
    assert "[pcli] Note" not in result.output


@pytest.mark.asyncio
async def test_write_todos_does_not_flag_when_nothing_was_completed(tmp_path: Path):
    session = Session()
    await WRITE_TODOS.handler(
        {"todos": [{"content": "A", "status": "pending"}, {"content": "B", "status": "in_progress"}]},
        _ctx(tmp_path, session),
    )

    result = await WRITE_TODOS.handler(
        {"todos": [{"content": "Completely different plan.", "status": "pending"}]},
        _ctx(tmp_path, session),
    )

    assert result.is_error is False
    assert "[pcli] Note" not in result.output  # nothing completed existed to lose


@pytest.mark.asyncio
async def test_write_todos_tolerates_minor_rewording_of_a_completed_item(tmp_path: Path):
    session = Session()
    await WRITE_TODOS.handler(
        {"todos": [{"content": "Load the California Housing dataset and save it to CSV.", "status": "completed"}]},
        _ctx(tmp_path, session),
    )

    # Same task, just tightened phrasing - not a real loss.
    result = await WRITE_TODOS.handler(
        {
            "todos": [
                {"content": "Load the California Housing dataset and save it to CSV.", "status": "completed"},
                {"content": "Preprocess the data.", "status": "in_progress"},
            ]
        },
        _ctx(tmp_path, session),
    )

    assert result.is_error is False
    assert "[pcli] Note" not in result.output


def test_old_session_without_todos_field_still_validates():
    """A session persisted before this field existed must still load fine."""
    import json

    raw = Session(model="fake-model").model_dump(mode="json")
    del raw["todos"]
    restored = Session.model_validate(raw)
    assert restored.todos == []
    json.dumps(raw)  # sanity: still valid JSON without the field present
