"""Common interface both sandbox backends implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExecRequest:
    command: list[str] | str
    """A list is executed directly (argv, no shell); a str is run through the
    platform shell (needed for pipes/redirection in ad hoc shell commands)."""
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    stdin: str | None = None
    network: bool = False


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    backend_used: str


@dataclass
class SandboxCapabilities:
    supports_network_isolation: bool
    supports_memory_limit: bool
    supports_cpu_limit: bool


class Sandbox(ABC):
    name: str = "base"

    @abstractmethod
    async def execute(self, request: ExecRequest) -> ExecResult: ...

    @abstractmethod
    def capabilities(self) -> SandboxCapabilities: ...


class SandboxSecurityError(Exception):
    """Raised when a request would escape the sandbox's containment (e.g. a
    cwd outside the allowed roots). Distinct from a command simply failing."""
