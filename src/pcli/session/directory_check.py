"""Warns (never blocks) when a session is resumed from a different directory
than where it was created - every relative path in its tool-call history
resolves against that original directory, so a mismatch is worth surfacing
before continuing, not silently ignoring. Deliberately its own tiny module
(not a method on Session) so both the TUI's session picker and the CLI's
--resume flag can share the exact same check without depending on Textual
or Typer."""

from __future__ import annotations

from pathlib import Path

from pcli.session.models import Session


def directory_mismatch(session: Session, cwd: Path) -> str | None:
    """None if there's nothing to warn about (no recorded working_dir, or it
    matches cwd); otherwise a human-readable warning message."""
    if not session.working_dir:
        return None
    stored = Path(session.working_dir)
    if stored.expanduser().resolve() == cwd.expanduser().resolve():
        return None
    return (
        f"This session was last run in {session.working_dir}, but you're currently in "
        f"{cwd}. Relative paths in its history (and any new tool calls) will resolve "
        "against the current directory, not the original one."
    )
