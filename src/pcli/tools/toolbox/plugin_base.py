"""Plugin architecture for curated per-software tool sets.

A plugin declares how to recognize its software (candidate binary names +
how to run/parse a version check) and, once detected, the specific
admin/ops/user commands it exposes as tools. Each CommandSpec carries its
own already-resolved `binary_path` rather than assuming one binary for the
whole plugin — most software (kubectl, apachectl) is one binary with many
subcommands, but some (Sun Grid Engine: qstat/qsub/qdel/...) is a suite of
separate binaries, and this shape covers both without special-casing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class DetectionResult:
    software_name: str
    binary_path: str
    """Path to the plugin's primary binary (used to confirm presence/version)."""
    version_string: str
    version: tuple[int, ...]


@dataclass
class CommandSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    """JSON schema for the tool's arguments."""
    build_args: Callable[[dict[str, Any]], list[str]]
    """Given validated tool arguments, returns the argv (after the binary)."""
    binary_path: str
    risk: Literal["read", "mutate", "destructive"] = "read"


class ToolboxPlugin(ABC):
    software_name: str
    binary_names: tuple[str, ...]
    """Candidate primary-binary names to look for on PATH, tried in order."""
    version_args: tuple[str, ...] = ("--version",)

    @abstractmethod
    def parse_version(self, version_output: str) -> tuple[int, ...] | None: ...

    @abstractmethod
    def build_tools(self, detection: DetectionResult) -> list[CommandSpec]: ...
