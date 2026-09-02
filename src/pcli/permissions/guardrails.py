"""Hard, non-negotiable, config-driven checks. A guardrail violation is an
automatic deny — it never reaches the permission-prompt UI. This is separate
from (and evaluated before) PermissionManager's ask/remember flow; see
manager.py.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pcli.config.paths import guardrails_file
from pcli.config.settings import _dump_toml

DEFAULT_GUARDRAILS_TOML = """\
# pcli guardrails — hard limits that are never prompted, only enforced.
# Patterns without '*'/'?' are matched as a case-insensitive substring of the
# command; patterns with '*'/'?' are matched anywhere in the command via
# shell-style wildcards. This is a heuristic safety net, not a shell parser —
# treat it as defense-in-depth alongside the sandbox, not a substitute for it.

[shell]
denylist = [
    "rm -rf /",
    "rm -rf ~",
    "mkfs*",
    ":(){ :|:& };:",
    "dd if=/dev/zero",
    "> /dev/sda",
]

[fs]
allowed_roots = ["."]
deny_paths = ["~/.ssh", "~/.aws", "~/.config/pcli"]

[limits]
max_output_bytes = 2000000
max_tool_calls_per_turn = 25
max_tool_calls_per_minute = 60
max_shell_timeout_s = 300
max_background_jobs = 5

[python]
# Top-level modules the pydiscovery tools (inspect_python_module, call_python)
# refuse to import/resolve into — these overlap with the dedicated, reviewed
# shell/fs tools, so letting the LLM reach them indirectly via arbitrary
# Python calls would be a redundant and higher-risk escape hatch.
module_denylist = [
    "os",
    "sys",
    "subprocess",
    "ctypes",
    "shutil",
    "socket",
    "importlib",
    "multiprocessing",
    "threading",
    "pty",
]
"""


class GuardrailResult(BaseModel):
    allowed: bool
    reason: str | None = None


def _pattern_matches(command: str, pattern: str) -> bool:
    normalized_command = command.strip().lower()
    normalized_pattern = pattern.strip().lower()
    if "*" in normalized_pattern or "?" in normalized_pattern:
        wrapped = normalized_pattern
        if not wrapped.startswith("*"):
            wrapped = f"*{wrapped}"
        if not wrapped.endswith("*"):
            wrapped = f"{wrapped}*"
        return fnmatch.fnmatch(normalized_command, wrapped)
    return normalized_pattern in normalized_command


class GuardrailsConfig(BaseModel):
    shell_denylist: list[str] = []
    fs_allowed_roots: list[str] = ["."]
    fs_deny_paths: list[str] = []
    max_output_bytes: int = 2_000_000
    max_tool_calls_per_turn: int = 25
    max_tool_calls_per_minute: int = 60
    max_shell_timeout_s: int = 300
    """Ceiling run_shell's (blocking) timeout_s is clamped to — commands
    that need longer should use run_shell_background instead, which has no
    such cap since it doesn't block the turn."""
    max_background_jobs: int = 5
    """Concurrent-running cap for run_shell_background; enforced by
    RestrictedSubprocessSandbox.start_background."""
    python_module_denylist: list[str] = []

    @classmethod
    def load(cls) -> GuardrailsConfig:
        path = guardrails_file()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_GUARDRAILS_TOML, encoding="utf-8")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        shell = raw.get("shell", {})
        fs = raw.get("fs", {})
        limits = raw.get("limits", {})
        python = raw.get("python", {})
        return cls(
            shell_denylist=shell.get("denylist", []),
            fs_allowed_roots=fs.get("allowed_roots", ["."]),
            fs_deny_paths=fs.get("deny_paths", []),
            max_output_bytes=limits.get("max_output_bytes", 2_000_000),
            max_tool_calls_per_turn=limits.get("max_tool_calls_per_turn", 25),
            max_tool_calls_per_minute=limits.get("max_tool_calls_per_minute", 60),
            max_shell_timeout_s=limits.get("max_shell_timeout_s", 300),
            max_background_jobs=limits.get("max_background_jobs", 5),
            python_module_denylist=python.get("module_denylist", []),
        )

    def evaluate_command(self, command: str) -> GuardrailResult:
        for pattern in self.shell_denylist:
            if _pattern_matches(command, pattern):
                return GuardrailResult(
                    allowed=False, reason=f"command matches denylist pattern '{pattern}'"
                )
        return GuardrailResult(allowed=True)

    def evaluate_python_module(self, qualified_name: str) -> GuardrailResult:
        top_level = qualified_name.split(".")[0]
        if top_level in self.python_module_denylist:
            return GuardrailResult(
                allowed=False,
                reason=f"module '{top_level}' is blocked (use the dedicated shell/fs tools instead)",
            )
        return GuardrailResult(allowed=True)

    def evaluate_path(self, path: str | Path) -> GuardrailResult:
        resolved = Path(path).expanduser().resolve()

        for deny_path in self.fs_deny_paths:
            deny_resolved = Path(deny_path).expanduser().resolve()
            if resolved == deny_resolved or _is_relative_to(resolved, deny_resolved):
                return GuardrailResult(
                    allowed=False, reason=f"path is within denied path '{deny_path}'"
                )

        for allowed_root in self.fs_allowed_roots:
            allowed_resolved = Path(allowed_root).expanduser().resolve()
            if resolved == allowed_resolved or _is_relative_to(resolved, allowed_resolved):
                return GuardrailResult(allowed=True)

        return GuardrailResult(
            allowed=False, reason=f"path '{resolved}' is outside all allowed roots"
        )


def update_guardrails_limits(**limit_updates: Any) -> None:
    """Persists the given key/value pairs into guardrails.toml's [limits]
    table, preserving the [shell]/[fs]/[python] tables and any other
    [limits] keys untouched — mirrors config/settings.py's
    update_config_file, but for guardrails.toml's fixed 4-table shape
    instead of config.toml's flatter one.

    Falsy values (None) are skipped rather than written, matching
    update_config_file's same behavior for optional callers."""
    path = guardrails_file()
    if path.exists():
        raw = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    else:
        raw = dict(tomllib.loads(DEFAULT_GUARDRAILS_TOML))
    limits = dict(raw.get("limits", {}))
    limits.update({key: value for key, value in limit_updates.items() if value is not None})
    raw["limits"] = limits
    path.write_text(_dump_toml(raw), encoding="utf-8")


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False
