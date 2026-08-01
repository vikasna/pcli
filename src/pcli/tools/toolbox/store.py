"""Persistent cache for toolbox discovery results.

Curated plugins re-detect live on every load (a `--version` probe is cheap
and catches upgrades) — nothing curated needs to be cached. Only the
LLM-synthesized schemas for uncurated software are cached, keyed by a hash
of the --help corpus they were generated from, so an unchanged install
doesn't re-trigger an LLM call on every startup.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pcli.config.paths import toolbox_dir


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def registry_path() -> Path:
    return toolbox_dir() / "registry.json"


def read_registry() -> dict[str, dict]:
    path = registry_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_registry(registry: dict[str, dict]) -> None:
    _atomic_write(registry_path(), json.dumps(registry, indent=2))


def synthesized_schema_path(software_name: str) -> Path:
    return toolbox_dir() / software_name / "synthesized.json"


def read_synthesized_schema(software_name: str) -> dict | None:
    path = synthesized_schema_path(software_name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_synthesized_schema(
    software_name: str, *, help_corpus_hash: str, version: str, tools: list[dict]
) -> None:
    _atomic_write(
        synthesized_schema_path(software_name),
        json.dumps(
            {"help_corpus_hash": help_corpus_hash, "version": version, "tools": tools}, indent=2
        ),
    )


def hash_corpus(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
