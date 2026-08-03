import json
from pathlib import Path

import httpx
import pytest
import respx

from pcli.agent.compaction import (
    _render_transcript,
    _system_prompt_prefix_len,
    _turn_boundaries,
    maybe_compact,
)
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.session.models import Message
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        max_retries=1,
    )


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _summary_response(text: str) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
    return httpx.Response(200, content=_sse(*chunks))


def _make_messages() -> list[Message]:
    return [
        Message(role="system", content="system prompt"),
        Message(role="user", content="turn 1 user"),
        Message(role="assistant", content="turn 1 assistant"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
        Message(role="user", content="turn 3 user"),
        Message(role="assistant", content="turn 3 assistant"),
    ]


def test_turn_boundaries_finds_user_message_indices():
    assert _turn_boundaries(_make_messages()) == [1, 3, 5]


def test_system_prompt_prefix_len():
    messages = _make_messages()
    assert _system_prompt_prefix_len(messages) == 1
    assert _system_prompt_prefix_len(messages[1:]) == 0
    assert _system_prompt_prefix_len([]) == 0


def test_system_prompt_prefix_len_does_not_count_a_run():
    """A previous compaction's summary is also role=='system' but sits at
    index 1, not 0 — it must not be swept into the protected prefix."""
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="system", content="prior compaction summary"),
        Message(role="user", content="new turn"),
    ]
    assert _system_prompt_prefix_len(messages) == 1


def test_render_transcript_includes_role_headers_and_content():
    text = _render_transcript(_make_messages()[1:3])
    assert "--- user ---" in text
    assert "turn 1 user" in text
    assert "--- assistant ---" in text
    assert "turn 1 assistant" in text


@pytest.mark.asyncio
async def test_maybe_compact_returns_none_when_not_enough_history(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = _make_messages()[:3]  # system prompt + exactly 1 turn
    artifact_store = SessionArtifactStore(store, session.id)

    async with GatewayClient(_settings()) as client:
        result = await maybe_compact(
            session,
            gateway_client=client,
            model="fake-model",
            artifact_store=artifact_store,
            keep_recent_turns=2,
        )

    assert result is None
    assert [(m.role, m.content) for m in session.messages] == [
        (m.role, m.content) for m in _make_messages()[:3]
    ]


@pytest.mark.asyncio
@respx.mock
async def test_maybe_compact_happy_path_archives_and_summarizes(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_summary_response("Summary of early turns.")
    )

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = _make_messages()
    artifact_store = SessionArtifactStore(store, session.id)

    async with GatewayClient(_settings()) as client:
        result = await maybe_compact(
            session,
            gateway_client=client,
            model="fake-model",
            artifact_store=artifact_store,
            keep_recent_turns=2,
        )

    assert result is not None
    assert result.messages_compacted == 2  # "turn 1 user" + "turn 1 assistant"

    # [system, summary, turn2_user, turn2_assistant, turn3_user, turn3_assistant]
    assert len(session.messages) == 6
    assert session.messages[0].content == "system prompt"
    assert session.messages[1].role == "system"
    assert "Summary of early turns." in session.messages[1].content
    assert f"artifact_id='{result.artifact_id}'" in session.messages[1].content
    assert session.messages[2].content == "turn 2 user"
    assert session.messages[-1].content == "turn 3 assistant"

    archived = artifact_store.get(result.artifact_id)
    assert archived is not None
    assert "turn 1 user" in archived
    assert "turn 1 assistant" in archived
    assert "turn 2 user" not in archived  # only the compacted range was archived


@pytest.mark.asyncio
@respx.mock
async def test_maybe_compact_recompaction_folds_prior_summary_without_special_casing(
    tmp_path: Path,
):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _summary_response("First summary."),
        _summary_response("Combined summary."),
    ]

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = _make_messages()
    artifact_store = SessionArtifactStore(store, session.id)

    async with GatewayClient(_settings()) as client:
        first = await maybe_compact(
            session,
            gateway_client=client,
            model="fake-model",
            artifact_store=artifact_store,
            keep_recent_turns=2,
        )
        assert first is not None

        session.messages.append(Message(role="user", content="turn 4 user"))
        session.messages.append(Message(role="assistant", content="turn 4 assistant"))

        second = await maybe_compact(
            session,
            gateway_client=client,
            model="fake-model",
            artifact_store=artifact_store,
            keep_recent_turns=2,
        )

    assert second is not None
    # The prior summary message + turn 2's 2 messages = 3, folded in with no special-casing.
    assert second.messages_compacted == 3
    assert session.messages[0].content == "system prompt"
    assert session.messages[1].content.startswith("Combined summary.")
    assert session.messages[2].content == "turn 3 user"
    assert route.call_count == 2

    # The second archive contains the first summary's text plus turn 2 — proving
    # the prior compaction wasn't silently dropped, just folded forward.
    archived_second = artifact_store.get(second.artifact_id)
    assert "First summary." in archived_second
    assert "turn 2 user" in archived_second
