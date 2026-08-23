from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import ToolContext
from pcli.tools.builtin.artifact_tool import FETCH_ARTIFACT


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("fetch_artifact should never touch the sandbox")


def _ctx(tmp_path: Path, artifact_store) -> ToolContext:
    return ToolContext(
        sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=tmp_path, artifact_store=artifact_store
    )


@pytest.mark.asyncio
async def test_fetch_artifact_without_store_reports_error(tmp_path: Path):
    result = await FETCH_ARTIFACT.handler({"artifact_id": "art_x"}, _ctx(tmp_path, None))
    assert result.is_error is True
    assert "No artifact store" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_unknown_id_reports_error(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    result = await FETCH_ARTIFACT.handler({"artifact_id": "art_missing"}, _ctx(tmp_path, artifacts))
    assert result.is_error is True
    assert "No artifact found" in result.output
    assert "[pcli] Suggestion:" in result.output
    assert "artifact_id=" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_returns_full_content_when_small(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)
    artifact_id = artifacts.put("hello world")

    result = await FETCH_ARTIFACT.handler({"artifact_id": artifact_id}, _ctx(tmp_path, artifacts))
    assert result.is_error is False
    assert result.output == "hello world"


@pytest.mark.asyncio
async def test_fetch_artifact_pages_with_offset_and_limit(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)
    content = "0123456789" * 1000  # 10,000 chars
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "offset": 0, "limit": 100}, _ctx(tmp_path, artifacts)
    )
    assert result.output.startswith(content[:100])
    assert "offset=100 for more" in result.output

    result2 = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "offset": 100, "limit": 100}, _ctx(tmp_path, artifacts)
    )
    assert content[100:200] in result2.output


@pytest.mark.asyncio
async def test_fetch_artifact_last_page_has_no_continuation_note(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)
    artifact_id = artifacts.put("short content")

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "limit": 10_000}, _ctx(tmp_path, artifacts)
    )
    assert "showing chars" not in result.output
