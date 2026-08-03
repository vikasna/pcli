from pathlib import Path

import pytest

from pcli.llm.models import Usage
from pcli.session.export import export_session
from pcli.session.importer import SessionImportError, import_session
from pcli.session.models import Message, PermissionGrant, Session, ToolInvocation, TurnCost
from pcli.session.store import SessionStore


def _build_session(store: SessionStore) -> Session:
    session = store.new_session(model="fake-model", gateway_base_url="http://gateway.test/v1")
    session.messages.append(Message(role="system", content="system prompt"))
    session.messages.append(Message(role="user", content="hello"))
    session.messages.append(Message(role="assistant", content="hi there"))

    invocation = ToolInvocation(
        tool_name="run_shell",
        arguments={"command": "echo hi"},
        result_summary="hi",
        status="ok",
        backend="subprocess",
        duration_ms=12.3,
    )
    blob_name = store.write_blob(session.id, invocation.id, "hi\n" * 1000)
    invocation.full_result_ref = blob_name
    session.tool_invocations.append(invocation)

    session.cost.turns.append(
        TurnCost(turn_index=0, model="fake-model", usage=Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8), cost_usd=0.001)
    )
    session.cost.session_total_usd = 0.001
    session.cost.total_tokens = 8

    session.permission_grants.append(
        PermissionGrant(tool_name="run_shell", scope="always", decision="allow")
    )
    session.metadata["cwd"] = "/tmp/project"
    store.save(session)
    return session


def test_export_import_roundtrip(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    original = _build_session(store)

    out_path = tmp_path / "export.pcli-session.json"
    export_session(original, out_path, store=store)
    assert out_path.exists()

    imported = import_session(out_path, store=SessionStore(base_dir=tmp_path / "sessions2"))

    # A new id is always assigned on import; original id is kept for traceability.
    assert imported.id != original.id
    assert imported.metadata["imported_from_id"] == original.id

    assert [m.role for m in imported.messages] == [m.role for m in original.messages]
    assert [m.content for m in imported.messages] == [m.content for m in original.messages]
    assert imported.cost.session_total_usd == original.cost.session_total_usd
    assert imported.cost.total_tokens == original.cost.total_tokens
    assert len(imported.tool_invocations) == 1
    assert imported.tool_invocations[0].tool_name == "run_shell"

    # Blob content round-trips too.
    store2 = SessionStore(base_dir=tmp_path / "sessions2")
    blob_name = imported.tool_invocations[0].full_result_ref
    blob = store2.read_blob(imported.id, blob_name)
    assert blob == "hi\n" * 1000

    # Permission grants are NOT restored by default (security: imported
    # sessions can't silently carry "always allow shell" onto a new machine).
    assert imported.permission_grants == []


def test_export_import_roundtrip_with_restore_grants(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    original = _build_session(store)
    out_path = tmp_path / "export.pcli-session.json"
    export_session(original, out_path, store=store)

    imported = import_session(
        out_path, store=SessionStore(base_dir=tmp_path / "sessions2"), restore_grants=True
    )
    assert len(imported.permission_grants) == 1
    assert imported.permission_grants[0].tool_name == "run_shell"


def test_export_import_roundtrip_carries_compaction_artifact(tmp_path: Path):
    """Compaction (agent/compaction.py) records its archived transcript via a
    synthetic ToolInvocation(tool_name="_compaction") purely so export_session
    (which only bundles blobs referenced via tool_invocations[*].full_result_ref)
    carries it along without needing its own bundling logic — verify that
    actually works end to end."""
    store = SessionStore(base_dir=tmp_path / "sessions")
    original = _build_session(store)

    compaction_invocation = ToolInvocation(
        tool_name="_compaction",
        arguments={},
        result_summary="Compacted 2 message(s).",
        status="ok",
    )
    blob_name = store.write_blob(original.id, compaction_invocation.id, "archived transcript text")
    compaction_invocation.full_result_ref = blob_name
    original.tool_invocations.append(compaction_invocation)
    store.save(original)

    out_path = tmp_path / "export.pcli-session.json"
    export_session(original, out_path, store=store)

    imported = import_session(out_path, store=SessionStore(base_dir=tmp_path / "sessions2"))

    imported_compaction = next(
        inv for inv in imported.tool_invocations if inv.tool_name == "_compaction"
    )
    store2 = SessionStore(base_dir=tmp_path / "sessions2")
    archived = store2.read_blob(imported.id, imported_compaction.full_result_ref)
    assert archived == "archived transcript text"


def test_import_rejects_newer_format_version(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    original = _build_session(store)
    out_path = tmp_path / "export.pcli-session.json"
    export_session(original, out_path, store=store)

    import json

    envelope = json.loads(out_path.read_text(encoding="utf-8"))
    envelope["format_version"] = 999
    out_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(SessionImportError):
        import_session(out_path, store=SessionStore(base_dir=tmp_path / "sessions3"))


def test_import_rejects_wrong_format(tmp_path: Path):
    import json

    bad_path = tmp_path / "not-a-session.json"
    bad_path.write_text(json.dumps({"format": "something-else"}), encoding="utf-8")
    with pytest.raises(SessionImportError):
        import_session(bad_path, store=SessionStore(base_dir=tmp_path / "sessions4"))


def test_gzip_export_roundtrip(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    original = _build_session(store)
    out_path = tmp_path / "export.pcli-session.json.gz"
    export_session(original, out_path, store=store)

    imported = import_session(out_path, store=SessionStore(base_dir=tmp_path / "sessions2"))
    assert imported.messages[1].content == "hello"


def test_store_list_index_and_delete(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    s1 = _build_session(store)
    s2 = store.new_session(model="fake-model")

    index = store.list_index()
    ids = {e.id for e in index}
    assert {s1.id, s2.id} <= ids

    store.delete(s1.id)
    ids_after = {e.id for e in store.list_index()}
    assert s1.id not in ids_after
    assert not (tmp_path / "sessions" / s1.id).exists()
