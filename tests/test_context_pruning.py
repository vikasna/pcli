"""Coverage for context_pruning.py: a lightweight, no-LLM-call pass that
shrinks old, already-resolved tool-result messages to a compact placeholder
— distinct from maybe_compact's coarse, threshold-triggered, LLM-summarized
full-turn compaction (tests/test_compaction.py)."""

from pathlib import Path

from pcli.agent.context_pruning import extract_purpose, prune_old_tool_results
from pcli.llm.models import ToolCall, ToolCallFunction
from pcli.session.models import Message, Session
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore


def _tool_call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(id=call_id, type="function", function=ToolCallFunction(name=name, arguments=arguments))


def _make_messages() -> list[Message]:
    """system prompt + 3 turns, each with one tool call/result pair —
    turn_boundaries indices land at [1, 4, 7], matching test_compaction.py's
    style but with tool round-trips added."""
    return [
        Message(role="system", content="system prompt"),
        Message(role="user", content="turn 1 user"),
        Message(
            role="assistant",
            tool_calls=[_tool_call("c1", "run_shell", '{"command":"which x","purpose":"check for x"}')],
        ),
        Message(role="tool", content="not found", tool_call_id="c1", name="run_shell"),
        Message(role="user", content="turn 2 user"),
        Message(
            role="assistant",
            tool_calls=[_tool_call("c2", "run_shell", '{"command":"ls"}')],
        ),
        Message(role="tool", content="file1 file2", tool_call_id="c2", name="run_shell"),
        Message(role="user", content="turn 3 user"),
        Message(
            role="assistant",
            tool_calls=[_tool_call("c3", "run_shell", '{"command":"pwd"}')],
        ),
        Message(role="tool", content="/home/user", tool_call_id="c3", name="run_shell"),
    ]


def _session_and_artifacts(tmp_path: Path) -> tuple[Session, SessionArtifactStore]:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    return session, SessionArtifactStore(store, session.id)


# --- extract_purpose ---


def test_extract_purpose_returns_the_purpose_string():
    assert extract_purpose('{"command":"ls","purpose":"list files"}') == "list files"


def test_extract_purpose_returns_none_when_absent():
    assert extract_purpose('{"command":"ls"}') is None


def test_extract_purpose_returns_none_for_malformed_json():
    assert extract_purpose("not json") is None


def test_extract_purpose_returns_none_for_non_string_purpose():
    assert extract_purpose('{"purpose": 123}') is None


def test_extract_purpose_returns_none_for_blank_purpose():
    assert extract_purpose('{"purpose": "   "}') is None


def test_extract_purpose_handles_empty_arguments():
    assert extract_purpose("") is None


# --- prune_old_tool_results ---


def test_prune_old_tool_results_keeps_the_most_recent_turn_verbatim(tmp_path: Path):
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()

    pruned_count = prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    assert pruned_count == 2  # turn 1's and turn 2's tool results
    assert session.messages[3].pruned_artifact_id is not None
    assert "Pruned tool result" in session.messages[3].content
    assert session.messages[6].pruned_artifact_id is not None
    # Turn 3 (most recent) is untouched.
    assert session.messages[9].content == "/home/user"
    assert session.messages[9].pruned_artifact_id is None


def test_prune_old_tool_results_embeds_purpose_in_the_placeholder(tmp_path: Path):
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()

    prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    assert "Purpose: check for x." in session.messages[3].content
    # turn 2's tool call had no purpose - placeholder must omit the clause cleanly.
    assert "Purpose:" not in session.messages[6].content


def test_prune_old_tool_results_archives_the_original_content_retrievably(tmp_path: Path):
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()

    prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    artifact_id = session.messages[3].pruned_artifact_id
    assert artifact_store.get(artifact_id) == "not found"
    assert f"artifact_id='{artifact_id}'" in session.messages[3].content


def test_prune_old_tool_results_is_idempotent(tmp_path: Path):
    """Safe to call every turn - an already-pruned message must not be
    re-archived (which would create a duplicate artifact file) or have its
    placeholder rewritten."""
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()

    first = prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)
    placeholder_after_first = session.messages[3].content
    artifact_id_after_first = session.messages[3].pruned_artifact_id

    second = prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    assert first == 2
    assert second == 0
    assert session.messages[3].content == placeholder_after_first
    assert session.messages[3].pruned_artifact_id == artifact_id_after_first


def test_prune_old_tool_results_no_op_when_not_enough_turns_yet(tmp_path: Path):
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()[:4]  # system + turn 1 only

    pruned_count = prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    assert pruned_count == 0
    assert session.messages[3].content == "not found"
    assert session.messages[3].pruned_artifact_id is None


def test_prune_old_tool_results_never_touches_non_tool_messages(tmp_path: Path):
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()

    prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    assert session.messages[0].content == "system prompt"
    assert session.messages[1].content == "turn 1 user"
    assert (
        session.messages[2].tool_calls[0].function.arguments
        == '{"command":"which x","purpose":"check for x"}'
    )
    assert session.messages[4].content == "turn 2 user"


def test_prune_old_tool_results_keep_recent_turns_zero_prunes_everything(tmp_path: Path):
    """A direct call with keep_recent_turns=0 prunes even the most recent
    turn - the /prune-tool-results slash command itself rejects 0 (use
    'off' instead), but the underlying function stays simple/permissive;
    this also guards against the classic Python `list[-0]` gotcha (which
    would otherwise silently index from the front instead of "keep none")."""
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()

    pruned_count = prune_old_tool_results(session, keep_recent_turns=0, artifact_store=artifact_store)

    assert pruned_count == 3
    assert session.messages[9].pruned_artifact_id is not None


def test_prune_old_tool_results_skips_tool_messages_with_empty_content(tmp_path: Path):
    session, artifact_store = _session_and_artifacts(tmp_path)
    session.messages = _make_messages()
    session.messages[3].content = None

    pruned_count = prune_old_tool_results(session, keep_recent_turns=1, artifact_store=artifact_store)

    assert pruned_count == 1  # only turn 2's tool result
    assert session.messages[3].content is None
    assert session.messages[3].pruned_artifact_id is None
