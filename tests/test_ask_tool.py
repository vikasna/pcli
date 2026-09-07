from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.ask_tool import ASK_USER_QUESTION
from pcli.tools.registry import build_default_registry


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("ask_user_question should never touch the sandbox")


def _ctx(tmp_path: Path, *, ask_question=None) -> ToolContext:
    return ToolContext(
        sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, ask_question=ask_question
    )


@pytest.mark.asyncio
async def test_ask_user_question_without_ui_reports_a_clear_error(tmp_path: Path):
    result = await ASK_USER_QUESTION.handler({"question": "Which port?"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "No UI available" in result.output
    assert "[pcli] Suggestion:" in result.output


@pytest.mark.asyncio
async def test_ask_user_question_returns_the_users_answer(tmp_path: Path):
    async def fake_ask_question(question: str, options: list[str] | None) -> str:
        assert question == "Which port should the server use?"
        assert options is None
        return "8080"

    result = await ASK_USER_QUESTION.handler(
        {"question": "Which port should the server use?"},
        _ctx(tmp_path, ask_question=fake_ask_question),
    )
    assert result.is_error is False
    assert result.output == "8080"


@pytest.mark.asyncio
async def test_ask_user_question_passes_options_through(tmp_path: Path):
    received: dict = {}

    async def fake_ask_question(question: str, options: list[str] | None) -> str:
        received["question"] = question
        received["options"] = options
        return "SQLite"

    result = await ASK_USER_QUESTION.handler(
        {
            "question": "Which database should this use?",
            "options": ["SQLite", "Postgres", "MySQL"],
        },
        _ctx(tmp_path, ask_question=fake_ask_question),
    )
    assert result.is_error is False
    assert result.output == "SQLite"
    assert received["options"] == ["SQLite", "Postgres", "MySQL"]


def test_ask_user_question_needs_no_permission_and_is_plan_mode_safe():
    """Asking is read-only (no side effects on disk/system) and useful even
    while restricted to plan mode - the whole point is clarifying intent
    before acting, not an action itself."""
    assert ASK_USER_QUESTION.needs_permission is False
    assert ASK_USER_QUESTION.plan_mode_safe is True


def test_ask_user_question_is_registered_in_the_default_registry():
    registry = build_default_registry()
    assert registry.get("ask_user_question") is not None
