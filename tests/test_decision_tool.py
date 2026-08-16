import json
from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.models import Session
from pcli.session.store import SessionStore
from pcli.tools.base import ToolContext
from pcli.tools.builtin.decision_tool import RECORD_DECISION, render_decisions


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("record_decision should never touch the sandbox")


def _ctx(tmp_path: Path, session: Session | None) -> ToolContext:
    return ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, session=session)


@pytest.mark.asyncio
async def test_record_decision_without_session_reports_error(tmp_path: Path):
    result = await RECORD_DECISION.handler(
        {"decision": "d", "rationale": "r"}, _ctx(tmp_path, None)
    )
    assert result.is_error is True
    assert "No session" in result.output


@pytest.mark.asyncio
async def test_record_decision_appends_to_session(tmp_path: Path):
    session = Session()
    result = await RECORD_DECISION.handler(
        {"decision": "Use httpx over requests", "rationale": "already an async dependency"},
        _ctx(tmp_path, session),
    )
    assert result.is_error is False
    assert len(session.decisions) == 1
    assert session.decisions[0].decision == "Use httpx over requests"
    assert session.decisions[0].rationale == "already an async dependency"
    assert "Use httpx over requests" in result.output


@pytest.mark.asyncio
async def test_record_decision_appends_rather_than_replaces(tmp_path: Path):
    """The key behavioral difference from write_todos: multiple calls
    accumulate an audit trail rather than overwriting it."""
    session = Session()
    await RECORD_DECISION.handler({"decision": "A", "rationale": "ra"}, _ctx(tmp_path, session))
    await RECORD_DECISION.handler({"decision": "B", "rationale": "rb"}, _ctx(tmp_path, session))
    await RECORD_DECISION.handler({"decision": "C", "rationale": "rc"}, _ctx(tmp_path, session))
    assert [d.decision for d in session.decisions] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_record_decision_requires_both_fields(tmp_path: Path):
    session = Session()
    missing_rationale = await RECORD_DECISION.handler({"decision": "d"}, _ctx(tmp_path, session))
    assert missing_rationale.is_error is True
    missing_decision = await RECORD_DECISION.handler({"rationale": "r"}, _ctx(tmp_path, session))
    assert missing_decision.is_error is True
    assert session.decisions == []  # rejected calls must not mutate session state


def test_render_decisions_empty():
    assert render_decisions([]) == "No decisions recorded."


def test_render_decisions_lists_each_entry():
    session = Session()
    import asyncio

    asyncio.run(
        RECORD_DECISION.handler(
            {"decision": "First", "rationale": "r1"},
            ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=Path("."), session=session),
        )
    )
    asyncio.run(
        RECORD_DECISION.handler(
            {"decision": "Second", "rationale": "r2"},
            ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=Path("."), session=session),
        )
    )
    rendered = render_decisions(session.decisions)
    assert "First" in rendered
    assert "Second" in rendered


def test_decisions_survive_session_export_import(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")

    import asyncio

    asyncio.run(
        RECORD_DECISION.handler(
            {"decision": "Use approach X", "rationale": "faster and already tested"},
            ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, session=session),
        )
    )
    store.save(session)

    out_path = tmp_path / "export.pcli-session.json"
    export_session(session, out_path, store=store)
    imported = import_session(out_path, store=SessionStore(base_dir=tmp_path / "sessions2"))

    assert len(imported.decisions) == 1
    assert imported.decisions[0].decision == "Use approach X"
    assert imported.decisions[0].rationale == "faster and already tested"


def test_old_session_without_decisions_field_still_validates():
    """A session persisted before this field existed must still load fine."""
    raw = Session(model="fake-model").model_dump(mode="json")
    del raw["decisions"]
    restored = Session.model_validate(raw)
    assert restored.decisions == []
    json.dumps(raw)  # sanity: still valid JSON without the field present
