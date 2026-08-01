"""Logging setup. File-based only — stdout/stderr are owned by the Textual TUI."""

from __future__ import annotations

import logging
from pathlib import Path

from pcli.config.paths import data_dir


def configure_logging(*, verbose: bool = False) -> Path:
    log_path = data_dir() / "pcli.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return log_path
