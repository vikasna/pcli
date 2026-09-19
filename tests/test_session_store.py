"""Coverage for SessionStore.prune_empty_sessions: new_session() saves
eagerly (before ChatScreen even appends the leading system prompt), so
closing pcli without ever sending a message leaves a useless, never-touched
entry cluttering /sessions — pruning deletes exactly those, nothing else."""

from pathlib import Path

from pcli.session.models import Message
from pcli.session.store import SessionStore


def _make_store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def test_prune_empty_sessions_deletes_a_never_touched_session(tmp_path: Path):
    store = _make_store(tmp_path)
    empty = store.new_session(model="fake-model")

    pruned = store.prune_empty_sessions()

    assert pruned == 1
    assert not store.session_dir(empty.id).exists()
    assert store.list_index() == []


def test_prune_empty_sessions_keeps_sessions_with_any_content(tmp_path: Path):
    store = _make_store(tmp_path)
    session = store.new_session(model="fake-model")
    session.messages.append(Message(role="system", content="system prompt"))
    store.save(session)

    pruned = store.prune_empty_sessions()

    assert pruned == 0
    assert store.session_dir(session.id).exists()
    assert len(store.list_index()) == 1


def test_prune_empty_sessions_excludes_given_ids_even_if_empty(tmp_path: Path):
    store = _make_store(tmp_path)
    active = store.new_session(model="fake-model")
    also_empty = store.new_session(model="fake-model")

    pruned = store.prune_empty_sessions(exclude_session_ids=[active.id])

    assert pruned == 1
    assert store.session_dir(active.id).exists()  # protected despite being empty
    assert not store.session_dir(also_empty.id).exists()


def test_prune_empty_sessions_only_removes_empty_ones_among_a_mix(tmp_path: Path):
    store = _make_store(tmp_path)
    empty_one = store.new_session(model="fake-model")
    empty_two = store.new_session(model="fake-model")
    real = store.new_session(model="fake-model")
    real.messages.append(Message(role="system", content="system prompt"))
    real.messages.append(Message(role="user", content="hello"))
    store.save(real)

    pruned = store.prune_empty_sessions()

    assert pruned == 2
    assert not store.session_dir(empty_one.id).exists()
    assert not store.session_dir(empty_two.id).exists()
    assert store.session_dir(real.id).exists()
    remaining_ids = {entry.id for entry in store.list_index()}
    assert remaining_ids == {real.id}


def test_prune_empty_sessions_is_a_noop_with_nothing_to_prune(tmp_path: Path):
    store = _make_store(tmp_path)
    assert store.prune_empty_sessions() == 0

    session = store.new_session(model="fake-model")
    session.messages.append(Message(role="system", content="system prompt"))
    store.save(session)

    assert store.prune_empty_sessions() == 0
    assert store.session_dir(session.id).exists()


def test_new_session_persists_working_dir(tmp_path: Path):
    store = _make_store(tmp_path)
    session = store.new_session(model="fake-model", working_dir="/some/project")

    reloaded = store.load(session.id)

    assert reloaded.working_dir == "/some/project"


def test_new_session_working_dir_defaults_to_none(tmp_path: Path):
    store = _make_store(tmp_path)
    session = store.new_session(model="fake-model")

    assert session.working_dir is None
    assert store.load(session.id).working_dir is None
