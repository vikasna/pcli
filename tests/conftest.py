"""Shared test isolation: every test gets its own private config/data/cache
directories, so nothing in the suite can read or write the real pcli
install on the machine running it.

This is a real (not hypothetical) bug class: test_llm_client.py's Settings()
calls don't explicitly override config_file(), so they fall through
Settings' own source precedence to _TomlFileSource, which reads whatever
config.toml actually exists on disk — on a machine where a real pcli
session has since persisted something via /timeout or /models, tests
started failing/flaking depending on the developer's own local state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcli.config import paths as paths_module


class _FakePlatformDirs:
    def __init__(self, base: Path) -> None:
        self.user_config_dir = str(base / "config")
        self.user_data_dir = str(base / "data")
        self.user_cache_dir = str(base / "cache")


@pytest.fixture(autouse=True)
def _isolated_pcli_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths_module, "_dirs", _FakePlatformDirs(tmp_path))
