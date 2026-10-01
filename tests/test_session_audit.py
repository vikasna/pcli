"""Coverage for session/audit.py's hash-chain primitives: append_audit_entry
and verify_audit_chain. The whole point of this module is tamper-evidence,
so these tests specifically cover detecting a modified/reordered/deleted
entry, not just the happy path."""

from pcli.session.audit import _GENESIS_HASH, append_audit_entry, verify_audit_chain
from pcli.session.models import Session


def _session() -> Session:
    return Session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")


def test_first_entry_chains_onto_the_genesis_hash():
    session = _session()

    entry = append_audit_entry(session, kind="tool_call", summary="read_file ok", detail={})

    assert entry.prev_hash == _GENESIS_HASH
    assert entry.entry_hash != _GENESIS_HASH
    assert len(entry.entry_hash) == 64  # a sha256 hex digest


def test_second_entry_chains_onto_the_first_entrys_hash():
    session = _session()

    first = append_audit_entry(session, kind="tool_call", summary="a", detail={})
    second = append_audit_entry(session, kind="tool_call", summary="b", detail={})

    assert second.prev_hash == first.entry_hash
    assert second.entry_hash != first.entry_hash


def test_entries_land_on_session_audit_log_in_order():
    session = _session()

    append_audit_entry(session, kind="tool_call", summary="a", detail={})
    append_audit_entry(session, kind="permission_decision", summary="b", detail={})

    assert [e.summary for e in session.audit_log] == ["a", "b"]


def test_identical_summary_and_detail_still_produce_different_hashes():
    """Regression guard: two entries with the same summary/detail must not
    collide just because their content looks the same - id/created_at/
    prev_hash (or at minimum prev_hash) must still differentiate them."""
    session = _session()

    first = append_audit_entry(session, kind="tool_call", summary="x", detail={"a": 1})
    second = append_audit_entry(session, kind="tool_call", summary="x", detail={"a": 1})

    assert first.entry_hash != second.entry_hash


# --- verify_audit_chain ---


def test_empty_log_is_valid():
    session = _session()

    result = verify_audit_chain(session)

    assert result.valid is True
    assert result.entry_count == 0
    assert result.broken_at_index is None


def test_untouched_chain_of_several_entries_is_valid():
    session = _session()
    for i in range(5):
        append_audit_entry(session, kind="tool_call", summary=f"entry {i}", detail={"i": i})

    result = verify_audit_chain(session)

    assert result.valid is True
    assert result.entry_count == 5


def test_mutating_an_old_entrys_detail_breaks_the_chain():
    session = _session()
    append_audit_entry(session, kind="tool_call", summary="a", detail={"path": "safe.txt"})
    append_audit_entry(session, kind="tool_call", summary="b", detail={})
    append_audit_entry(session, kind="tool_call", summary="c", detail={})

    session.audit_log[0].detail["path"] = "tampered.txt"

    result = verify_audit_chain(session)

    assert result.valid is False
    assert result.broken_at_index == 0
    assert "modified" in result.reason


def test_mutating_an_old_entrys_summary_breaks_the_chain():
    session = _session()
    append_audit_entry(session, kind="tool_call", summary="original", detail={})
    append_audit_entry(session, kind="tool_call", summary="next", detail={})

    session.audit_log[0].summary = "rewritten"

    result = verify_audit_chain(session)

    assert result.valid is False
    assert result.broken_at_index == 0


def test_deleting_an_entry_breaks_the_chain_via_prev_hash_mismatch():
    session = _session()
    append_audit_entry(session, kind="tool_call", summary="a", detail={})
    append_audit_entry(session, kind="tool_call", summary="b", detail={})
    append_audit_entry(session, kind="tool_call", summary="c", detail={})

    del session.audit_log[1]  # splice out the middle entry

    result = verify_audit_chain(session)

    assert result.valid is False
    assert result.broken_at_index == 1  # "c" now claims to follow "a", but "a" says otherwise
    assert "prev_hash" in result.reason


def test_reordering_entries_breaks_the_chain():
    session = _session()
    append_audit_entry(session, kind="tool_call", summary="a", detail={})
    append_audit_entry(session, kind="tool_call", summary="b", detail={})

    session.audit_log[0], session.audit_log[1] = session.audit_log[1], session.audit_log[0]

    result = verify_audit_chain(session)

    assert result.valid is False
    assert result.broken_at_index == 0


def test_corruption_is_reported_at_the_first_broken_entry_not_the_last():
    session = _session()
    for i in range(4):
        append_audit_entry(session, kind="tool_call", summary=f"entry {i}", detail={})

    session.audit_log[1].summary = "tampered"

    result = verify_audit_chain(session)

    assert result.broken_at_index == 1
