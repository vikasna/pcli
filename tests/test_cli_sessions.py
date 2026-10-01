"""Coverage for `pcli sessions verify` - recomputes and validates a
session's audit_log hash chain (session/audit.py). `pcli sessions list/
export/import` aren't covered here or anywhere else in the test suite -
a pre-existing gap, not something this file's own scope extends to.

Uses SessionStore() with no explicit base_dir throughout, matching exactly
what the CLI command itself constructs internally - tests/conftest.py's
autouse isolation fixture already redirects data_dir() per test, so a
custom base_dir here would point at a different directory than the CLI
subprocess-equivalent call resolves to, and 'pcli sessions verify' would
never find the session this test just wrote."""

from pathlib import Path

from typer.testing import CliRunner

from pcli.cli import app
from pcli.session.audit import append_audit_entry
from pcli.session.store import SessionStore

runner = CliRunner()


def test_sessions_verify_reports_an_empty_chain_as_valid(tmp_path: Path):
    store = SessionStore()
    session = store.new_session(model="fake-model", gateway_base_url="http://x")
    store.save(session)

    result = runner.invoke(app, ["sessions", "verify", session.id])

    assert result.exit_code == 0
    assert "0 audit entries, chain valid" in result.output


def test_sessions_verify_reports_a_valid_chain(tmp_path: Path):
    store = SessionStore()
    session = store.new_session(model="fake-model", gateway_base_url="http://x")
    append_audit_entry(session, kind="tool_call", summary="a", detail={})
    append_audit_entry(session, kind="permission_decision", summary="b", detail={})
    store.save(session)

    result = runner.invoke(app, ["sessions", "verify", session.id])

    assert result.exit_code == 0
    assert "2 audit entries, chain valid" in result.output


def test_sessions_verify_detects_a_corrupted_chain(tmp_path: Path):
    store = SessionStore()
    session = store.new_session(model="fake-model", gateway_base_url="http://x")
    append_audit_entry(session, kind="tool_call", summary="a", detail={"path": "safe.txt"})
    append_audit_entry(session, kind="tool_call", summary="b", detail={})
    store.save(session)

    # Hand-corrupt the stored session file, as if someone edited the
    # record directly on disk after it was written.
    reloaded = store.load(session.id)
    reloaded.audit_log[0].detail["path"] = "tampered.txt"
    store.save(reloaded)

    result = runner.invoke(app, ["sessions", "verify", session.id])

    assert result.exit_code == 1
    assert "Chain broken at entry 0" in result.output
    assert "modified" in result.output


def test_sessions_verify_unknown_session_id_errors_cleanly(tmp_path: Path):
    result = runner.invoke(app, ["sessions", "verify", "sess_does_not_exist"])

    assert result.exit_code == 1
    assert "No session found" in result.output
