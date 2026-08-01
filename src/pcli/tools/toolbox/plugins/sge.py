"""Curated plugin for Sun Grid Engine / Univa Grid Engine — unlike kubectl or
httpd, this is a suite of separate binaries (qstat/qsub/qdel/qacct/qhost),
so build_tools() resolves each sibling independently and only includes
commands whose binary is actually present."""

from __future__ import annotations

import re
import shutil

from pcli.tools.toolbox.plugin_base import CommandSpec, DetectionResult, ToolboxPlugin

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _qstat_args(args: dict) -> list[str]:
    argv = []
    if args.get("all_users"):
        argv += ["-u", args.get("user") or "*"]
    if args.get("job_id"):
        argv += ["-j", str(args["job_id"])]
    if args.get("full"):
        argv.append("-f")
    return argv


def _qhost_args(args: dict) -> list[str]:
    return ["-q"] if args.get("queues") else []


def _qacct_args(args: dict) -> list[str]:
    return ["-j", str(args["job_id"])]


def _qsub_args(args: dict) -> list[str]:
    argv: list[str] = []
    if args.get("queue"):
        argv += ["-q", args["queue"]]
    if args.get("job_name"):
        argv += ["-N", args["job_name"]]
    argv.append(args["script"])
    return argv


def _qdel_args(args: dict) -> list[str]:
    return [str(args["job_id"])]


class SgePlugin(ToolboxPlugin):
    software_name = "sge"
    binary_names = ("qstat",)
    version_args = ("-help",)

    def parse_version(self, version_output: str) -> tuple[int, ...] | None:
        match = _VERSION_RE.search(version_output)
        if not match:
            # `qstat -help` doesn't reliably print a parseable version across SGE/UGE
            # forks; treat any output at all as "present, version unknown".
            return (0,)
        return tuple(int(g) for g in match.groups())

    def build_tools(self, detection: DetectionResult) -> list[CommandSpec]:
        specs: list[CommandSpec] = [
            CommandSpec(
                name="qstat",
                description="Show the status of scheduled/running jobs in the grid.",
                parameters={
                    "type": "object",
                    "properties": {
                        "all_users": {"type": "boolean"},
                        "user": {"type": "string"},
                        "job_id": {"type": "integer"},
                        "full": {"type": "boolean", "description": "Full job/queue detail."},
                    },
                },
                build_args=_qstat_args,
                binary_path=detection.binary_path,
                risk="read",
            )
        ]

        if qhost := shutil.which("qhost"):
            specs.append(
                CommandSpec(
                    name="qhost",
                    description="Show the status of execution hosts in the grid.",
                    parameters={"type": "object", "properties": {"queues": {"type": "boolean"}}},
                    build_args=_qhost_args,
                    binary_path=qhost,
                    risk="read",
                )
            )

        if qacct := shutil.which("qacct"):
            specs.append(
                CommandSpec(
                    name="qacct",
                    description="Show accounting/history information for a completed job.",
                    parameters={
                        "type": "object",
                        "properties": {"job_id": {"type": "integer"}},
                        "required": ["job_id"],
                    },
                    build_args=_qacct_args,
                    binary_path=qacct,
                    risk="read",
                )
            )

        if qsub := shutil.which("qsub"):
            specs.append(
                CommandSpec(
                    name="qsub",
                    description="Submit a job script to the grid scheduler.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "script": {"type": "string", "description": "Path to the job script."},
                            "queue": {"type": "string"},
                            "job_name": {"type": "string"},
                        },
                        "required": ["script"],
                    },
                    build_args=_qsub_args,
                    binary_path=qsub,
                    risk="mutate",
                )
            )

        if qdel := shutil.which("qdel"):
            specs.append(
                CommandSpec(
                    name="qdel",
                    description="Delete/cancel a submitted job.",
                    parameters={
                        "type": "object",
                        "properties": {"job_id": {"type": "integer"}},
                        "required": ["job_id"],
                    },
                    build_args=_qdel_args,
                    binary_path=qdel,
                    risk="destructive",
                )
            )

        return specs
