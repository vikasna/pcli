from pathlib import Path

from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore


def test_put_get_roundtrip(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    artifact_id = artifacts.put("a" * 10_000)
    assert artifact_id.startswith("art_")
    assert artifacts.get(artifact_id) == "a" * 10_000


def test_get_unknown_artifact_returns_none(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)
    assert artifacts.get("art_does_not_exist") is None


def test_each_put_gets_a_distinct_id(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    id1 = artifacts.put("content one")
    id2 = artifacts.put("content two")
    assert id1 != id2
    assert artifacts.get(id1) == "content one"
    assert artifacts.get(id2) == "content two"


def test_blob_name_for_matches_write_blob_named_convention(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model")
    artifacts = SessionArtifactStore(store, session.id)

    artifact_id = artifacts.put("hello")
    blob_name = SessionArtifactStore.blob_name_for(artifact_id)
    # Readable directly through the underlying store using the derived blob name.
    assert store.read_blob(session.id, blob_name) == "hello"
