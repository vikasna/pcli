"""Enumerates importable modules/packages by NAME only — via pkgutil and
importlib.metadata, neither of which imports the module bodies. This is the
"tier 1" index: cheap, safe, always available, good for coarse search.
Docstrings/signatures ("tier 2") require actually importing a specific
module, done on demand by inspect_python_module (see search.py)."""

from __future__ import annotations

import pkgutil
import sys
from dataclasses import dataclass
from importlib import metadata as importlib_metadata


@dataclass
class ModuleEntry:
    name: str
    package: str | None
    summary: str


def build_index() -> list[ModuleEntry]:
    entries: dict[str, ModuleEntry] = {}

    package_summaries: dict[str, str] = {}
    for dist in importlib_metadata.distributions():
        name = dist.metadata.get("Name") or dist.metadata.get("name")
        if not name:
            continue
        summary = dist.metadata.get("Summary") or ""
        package_summaries[name.lower().replace("-", "_")] = summary
        entries.setdefault(name, ModuleEntry(name=name, package=name, summary=summary))

    for name in sorted(getattr(sys, "stdlib_module_names", ())):
        if name.startswith("_"):
            continue
        entries.setdefault(
            name, ModuleEntry(name=name, package=None, summary="(Python standard library)")
        )

    for module_info in pkgutil.iter_modules():
        name = module_info.name
        if name.startswith("_"):
            continue
        summary = package_summaries.get(name.lower().replace("-", "_"), "")
        entries.setdefault(name, ModuleEntry(name=name, package=None, summary=summary))

    return sorted(entries.values(), key=lambda e: e.name)
