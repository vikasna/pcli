"""Disk cache for the pydiscovery index, invalidated by an environment
fingerprint (interpreter path + installed distribution name/version pairs)
so it rebuilds automatically when the venv changes, but not on every launch."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from importlib import metadata as importlib_metadata
from pathlib import Path

from pcli.config.paths import cache_dir
from pcli.tools.pydiscovery.index import ModuleEntry, build_index


def environment_fingerprint() -> str:
    dist_pairs = sorted(
        f"{(dist.metadata.get('Name') or dist.metadata.get('name') or '?')}=={dist.version}"
        for dist in importlib_metadata.distributions()
    )
    raw = "\n".join([sys.executable, sys.version, *dist_pairs])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def index_cache_path() -> Path:
    return cache_dir() / "pydiscovery_index.json"


def load_or_build_index(*, force_rebuild: bool = False) -> list[ModuleEntry]:
    fingerprint = environment_fingerprint()
    path = index_cache_path()

    if not force_rebuild and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("fingerprint") == fingerprint:
                return [ModuleEntry(**entry) for entry in data["entries"]]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # corrupt/stale cache -> fall through and rebuild

    entries = build_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"fingerprint": fingerprint, "entries": [asdict(e) for e in entries]}, indent=2
        ),
        encoding="utf-8",
    )
    return entries
