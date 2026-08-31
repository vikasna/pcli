from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import ToolContext
from pcli.tools.builtin.artifact_tool import _MAX_PATTERN_MATCHES, FETCH_ARTIFACT


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


def _make_artifacts(tmp_path: Path) -> SessionArtifactStore:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    return SessionArtifactStore(store, session.id)


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


# --- Pattern search (grep-style) ---
#
# Regression coverage for a real debugged case: an investigation session
# needed to hunt for a specific section (e.g. "pod counts for namespace X")
# buried in a large archived blob, and blind offset/limit pagination alone
# cost several extra fetch_artifact round-trips finding it.


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_finds_matching_line_with_context(tmp_path: Path):
    artifacts = _make_artifacts(tmp_path)
    content = "\n".join(f"line {i}" for i in range(20))
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "line 10", "context_lines": 1},
        _ctx(tmp_path, artifacts),
    )
    assert result.is_error is False
    assert "1 matching line(s)" in result.output
    assert "10: line 9" in result.output
    assert "11: line 10" in result.output
    assert "12: line 11" in result.output
    assert "line 5" not in result.output  # outside the context window


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_no_matches(tmp_path: Path):
    artifacts = _make_artifacts(tmp_path)
    artifact_id = artifacts.put("hello world")

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "nonexistent-xyz"}, _ctx(tmp_path, artifacts)
    )
    assert result.is_error is False
    assert "No lines matching" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_invalid_regex_reports_clean_error(tmp_path: Path):
    artifacts = _make_artifacts(tmp_path)
    artifact_id = artifacts.put("hello world")

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "("}, _ctx(tmp_path, artifacts)
    )
    assert result.is_error is True
    assert "Invalid regex" in result.output
    assert "[pcli] Suggestion:" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_merges_overlapping_context_windows(tmp_path: Path):
    """Two matches close enough together that their context windows
    overlap must render as one merged block, not duplicate the shared
    lines - standard grep -C behavior."""
    artifacts = _make_artifacts(tmp_path)
    lines = [f"line {i}" for i in range(10)]
    lines[3] = "MATCH-A"
    lines[5] = "MATCH-B"
    content = "\n".join(lines)
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "MATCH-", "context_lines": 2},
        _ctx(tmp_path, artifacts),
    )
    assert result.is_error is False
    assert "2 matching line(s)" in result.output
    # Windows for MATCH-A (idx 3, +-2 -> 1..5) and MATCH-B (idx 5, +-2 ->
    # 3..7) overlap - must merge into one block (no "--" separator, no
    # duplicated "line 3"/"line 5" content).
    assert result.output.count("--") == 0
    assert result.output.count("line 3") == 0  # overwritten by MATCH-A itself
    assert "MATCH-A" in result.output
    assert "MATCH-B" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_separates_distant_matches_with_marker(tmp_path: Path):
    artifacts = _make_artifacts(tmp_path)
    lines = [f"line {i}" for i in range(30)]
    lines[2] = "FIRST-MATCH"
    lines[27] = "SECOND-MATCH"
    content = "\n".join(lines)
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "MATCH", "context_lines": 1},
        _ctx(tmp_path, artifacts),
    )
    assert result.is_error is False
    assert "2 matching line(s)" in result.output
    assert "\n--\n" in result.output  # distinct, non-adjacent blocks


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_caps_matches_and_notes_it(tmp_path: Path):
    artifacts = _make_artifacts(tmp_path)
    content = "\n".join(f"MATCH {i}" for i in range(_MAX_PATTERN_MATCHES + 10))
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "MATCH", "context_lines": 0},
        _ctx(tmp_path, artifacts),
    )
    assert result.is_error is False
    assert f"{_MAX_PATTERN_MATCHES + 10} matching line(s)" in result.output
    assert f"showing first {_MAX_PATTERN_MATCHES}" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_respects_limit_as_output_cap(tmp_path: Path):
    artifacts = _make_artifacts(tmp_path)
    content = "\n".join(f"MATCH line {i} " + "x" * 50 for i in range(50))
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "MATCH", "context_lines": 0, "limit": 200},
        _ctx(tmp_path, artifacts),
    )
    assert result.is_error is False
    assert "[...output truncated to max_chars...]" in result.output


@pytest.mark.asyncio
async def test_fetch_artifact_pattern_ignores_offset(tmp_path: Path):
    """offset only applies to the plain character-slice path - it must be
    silently ignored (not cause an error) when pattern is also given."""
    artifacts = _make_artifacts(tmp_path)
    content = "\n".join(f"line {i}" for i in range(10))
    artifact_id = artifacts.put(content)

    result = await FETCH_ARTIFACT.handler(
        {"artifact_id": artifact_id, "pattern": "line 5", "offset": 500},
        _ctx(tmp_path, artifacts),
    )
    assert result.is_error is False
    assert "1 matching line(s)" in result.output
