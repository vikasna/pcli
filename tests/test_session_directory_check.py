from pathlib import Path

from pcli.session.directory_check import directory_mismatch
from pcli.session.models import Session


def test_no_warning_when_working_dir_matches_cwd(tmp_path: Path):
    session = Session(working_dir=str(tmp_path))
    assert directory_mismatch(session, tmp_path) is None


def test_no_warning_when_working_dir_is_unset():
    session = Session(working_dir=None)
    assert directory_mismatch(session, Path("/somewhere")) is None


def test_warns_when_working_dir_differs_from_cwd(tmp_path: Path):
    other = tmp_path / "other-project"
    other.mkdir()
    session = Session(working_dir=str(tmp_path))
    warning = directory_mismatch(session, other)
    assert warning is not None
    assert str(tmp_path) in warning
    assert str(other) in warning


def test_no_warning_for_equivalent_paths_written_differently(tmp_path: Path):
    """A trailing slash / relative-vs-resolved spelling difference isn't a
    real directory change - only resolved-path inequality is."""
    session = Session(working_dir=str(tmp_path) + "/")
    assert directory_mismatch(session, tmp_path) is None
