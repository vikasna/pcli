from pathlib import Path

import pytest
from typer.testing import CliRunner

from pcli.cli import _print_resume_hint, app
from pcli.config import paths as paths_module
from pcli.session.models import Message
from pcli.session.store import SessionStore

runner = CliRunner()


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(paths_module, "data_dir", lambda: data_dir)
    # SessionStore() (what cli.py itself constructs) resolves its base_dir
    # via paths.sessions_dir(), which calls the now-patched data_dir() - the
    # store instance handed back here targets the identical location.
    return SessionStore(base_dir=data_dir / "sessions")


def test_resume_unknown_session_id_errors_without_launching_the_tui(isolated_store: SessionStore):
    result = runner.invoke(app, ["--resume", "sess_does_not_exist"])

    assert result.exit_code == 1
    assert "No session found with id 'sess_does_not_exist'" in result.output


def test_resume_with_mismatched_working_dir_prompts_and_declining_exits_cleanly(
    isolated_store: SessionStore,
):
    session = isolated_store.new_session(model="fake-model", working_dir="/some/other/project")
    session.messages.append(Message(role="user", content="hi"))
    isolated_store.save(session)

    result = runner.invoke(app, ["--resume", session.id], input="n\n")

    assert result.exit_code == 0
    assert "Continue anyway?" in result.output


def test_resume_with_mismatched_working_dir_confirming_launches_the_app(
    isolated_store: SessionStore, monkeypatch: pytest.MonkeyPatch
):
    from pcli.tui import app as app_module

    session = isolated_store.new_session(model="fake-model", working_dir="/some/other/project")
    session.messages.append(Message(role="user", content="hi"))
    isolated_store.save(session)

    launched: list[object] = []

    class _FakePcliApp:
        def __init__(self, settings, *, session=None) -> None:
            launched.append(session)

        def run(self) -> None:
            pass

    monkeypatch.setattr(app_module, "PcliApp", _FakePcliApp)

    result = runner.invoke(app, ["--resume", session.id], input="y\n")

    assert result.exit_code == 0
    assert len(launched) == 1
    assert launched[0].id == session.id


def test_print_resume_hint_names_the_most_recently_updated_non_empty_session(
    isolated_store: SessionStore, capsys: pytest.CaptureFixture[str]
):
    older = isolated_store.new_session(model="fake-model")
    older.messages.append(Message(role="user", content="hi"))
    isolated_store.save(older)

    newer = isolated_store.new_session(model="fake-model")
    newer.messages.append(Message(role="user", content="hi"))
    isolated_store.save(newer)

    _print_resume_hint(isolated_store)

    captured = capsys.readouterr()
    assert f"pcli --resume {newer.id}" in captured.out
    assert older.id not in captured.out


def test_print_resume_hint_says_nothing_for_an_empty_session(
    isolated_store: SessionStore, capsys: pytest.CaptureFixture[str]
):
    isolated_store.new_session(model="fake-model")  # never got a message

    _print_resume_hint(isolated_store)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_print_resume_hint_says_nothing_with_no_sessions_at_all(
    isolated_store: SessionStore, capsys: pytest.CaptureFixture[str]
):
    _print_resume_hint(isolated_store)

    captured = capsys.readouterr()
    assert captured.out == ""
