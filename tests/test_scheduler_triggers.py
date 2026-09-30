"""Coverage for scheduler/triggers.py's two signature functions - the
"is anything new since the last poll" checks scheduler/daemon.py uses for
the "file_change"/"git_commit" ScheduleJob trigger kinds, the event-based
counterpart to compute_next_run's cron-due check."""

import subprocess
from pathlib import Path

from pcli.scheduler.triggers import compute_path_signature, get_git_head_sha


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


# --- compute_path_signature ---


def test_missing_path_gets_a_constant_sentinel(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert compute_path_signature(missing) == compute_path_signature(missing)


def test_a_path_reappearing_changes_the_signature(tmp_path: Path):
    target = tmp_path / "watched.txt"
    missing_signature = compute_path_signature(target)

    target.write_text("hello", encoding="utf-8")

    assert compute_path_signature(target) != missing_signature


def test_unchanged_file_has_a_stable_signature(tmp_path: Path):
    target = tmp_path / "watched.txt"
    target.write_text("hello", encoding="utf-8")

    assert compute_path_signature(target) == compute_path_signature(target)


def test_file_content_change_changes_the_signature(tmp_path: Path):
    target = tmp_path / "watched.txt"
    target.write_text("hello", encoding="utf-8")
    before = compute_path_signature(target)

    target.write_text("hello, world - much longer now", encoding="utf-8")

    assert compute_path_signature(target) != before


def test_directory_signature_changes_when_a_file_is_added(tmp_path: Path):
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "a.txt").write_text("a", encoding="utf-8")
    before = compute_path_signature(watched)

    (watched / "b.txt").write_text("b", encoding="utf-8")

    assert compute_path_signature(watched) != before


def test_directory_signature_changes_when_a_nested_file_changes(tmp_path: Path):
    watched = tmp_path / "watched"
    nested = watched / "sub"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("a", encoding="utf-8")
    before = compute_path_signature(watched)

    (nested / "a.txt").write_text("a, but different now", encoding="utf-8")

    assert compute_path_signature(watched) != before


def test_dot_git_directory_is_ignored(tmp_path: Path):
    """Watching a repo's working tree must not fire on the repo's own
    internal bookkeeping (index/objects churn from unrelated git commands)."""
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "a.txt").write_text("a", encoding="utf-8")
    before = compute_path_signature(watched)

    git_dir = watched / ".git"
    git_dir.mkdir()
    (git_dir / "some-internal-file").write_text("git internals", encoding="utf-8")

    assert compute_path_signature(watched) == before


# --- get_git_head_sha ---


def test_returns_the_current_head_sha(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "first commit")

    sha = get_git_head_sha(repo, None)

    assert sha is not None
    assert len(sha) == 40  # a full commit SHA


def test_sha_changes_after_a_new_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "first commit")
    before = get_git_head_sha(repo, None)

    (repo / "file.txt").write_text("v2", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "second commit")

    assert get_git_head_sha(repo, None) != before


def test_returns_none_for_a_non_repo_path(tmp_path: Path):
    not_a_repo = tmp_path / "just-a-dir"
    not_a_repo.mkdir()

    assert get_git_head_sha(not_a_repo, None) is None


def test_returns_none_for_an_unknown_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "first commit")

    assert get_git_head_sha(repo, "no-such-branch") is None


def test_specific_branch_resolves_independently_of_head(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "file.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "on main")
    main_sha = get_git_head_sha(repo, "main")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("v2", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "on feature")

    # HEAD (bare, no branch given) now follows "feature"...
    assert get_git_head_sha(repo, None) != main_sha
    # ...but "main" itself is unchanged and still resolvable directly.
    assert get_git_head_sha(repo, "main") == main_sha
