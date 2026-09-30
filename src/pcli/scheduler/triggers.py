"""Signature computation for the two event-based ScheduleJob trigger kinds
(scheduler/models.py's "file_change"/"git_commit") - both are polled by
scheduler/daemon.py exactly like a cron job's next_run_at, just comparing a
cheap signature instead of a timestamp. No new dependency: file signatures
are plain os.stat() calls (no file contents read, binary-safe), and git
commit lookups just shell out to `git rev-parse` - there's nothing here a
file-watching library or GitPython would meaningfully simplify for a single
poll-driven check."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_MISSING_PATH_SENTINEL = "missing"


def compute_path_signature(path: Path) -> str:
    """A sha256 hex digest over the sorted (relative_path, mtime_ns, size)
    of every file under `path` (or just `path` itself if it's a file) -
    changes whenever a watched file is added, removed, or modified,
    without reading any file's actual contents. Any path component named
    ".git" is skipped, so watching a repo's working tree doesn't fire on
    the repo's own internal bookkeeping. A path that doesn't exist yet (or
    no longer exists) gets a constant sentinel signature, so "still
    missing" is never treated as a change - only the path reappearing is."""
    if not path.exists():
        return _MISSING_PATH_SENTINEL

    if path.is_file():
        files = [path]
    else:
        files = sorted(p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts)

    entries = []
    for file_path in files:
        try:
            stat = file_path.stat()
        except OSError:
            # Deleted between the rglob listing and this stat() call -
            # treat as simply absent from this signature, not an error.
            continue
        try:
            relative = file_path.relative_to(path)
        except ValueError:
            relative = file_path
        entries.append(f"{relative.as_posix()}:{stat.st_mtime_ns}:{stat.st_size}")

    digest = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8"))
    return digest.hexdigest()


def get_git_head_sha(repo_path: Path, branch: str | None) -> str | None:
    """Runs `git rev-parse {branch or 'HEAD'}` in repo_path. Returns None
    (logged as a warning, not raised) if git isn't installed, repo_path
    isn't a git repo, or branch doesn't exist - the daemon just skips that
    job for this poll rather than marking it an error, since this is
    typically a transient/config issue that resolves itself (e.g. the repo
    hasn't been cloned yet) rather than a real job failure."""
    ref = branch or "HEAD"
    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Couldn't run git in %s: %s", repo_path, exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "git rev-parse %s failed in %s: %s", ref, repo_path, result.stderr.strip()
        )
        return None

    return result.stdout.strip()
