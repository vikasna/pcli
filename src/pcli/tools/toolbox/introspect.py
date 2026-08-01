"""Generic `--help` walker for software without a curated plugin. Best-effort
text corpus collection that feeds into synthesize.py — not a man-page/argparse
parser, just enough structure-guessing to find likely subcommands."""

from __future__ import annotations

import re
from pathlib import Path

from pcli.sandbox.base import ExecRequest
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox

_SUBCOMMAND_RE = re.compile(r"^\s{2,4}([a-z][a-z0-9_-]{1,30})\s{2,}\S", re.MULTILINE)
_MAX_SUBCOMMANDS = 8


async def _run_help(binary_path: str, args: list[str], cwd: Path) -> str:
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[cwd])
    result = await sandbox.execute(ExecRequest(command=[binary_path, *args], cwd=cwd, timeout_s=10))
    return (result.stdout + "\n" + result.stderr).strip()


def _guess_subcommands(help_text: str) -> list[str]:
    seen: list[str] = []
    for match in _SUBCOMMAND_RE.finditer(help_text):
        word = match.group(1)
        if word not in seen:
            seen.append(word)
    return seen[:_MAX_SUBCOMMANDS]


async def collect_help_corpus(binary_path: str, cwd: Path) -> str:
    top_help = await _run_help(binary_path, ["--help"], cwd)
    parts = [f"$ {binary_path} --help\n{top_help}"]

    for subcommand in _guess_subcommands(top_help):
        sub_help = await _run_help(binary_path, [subcommand, "--help"], cwd)
        if sub_help:
            parts.append(f"$ {binary_path} {subcommand} --help\n{sub_help}")

    return "\n\n".join(parts)
