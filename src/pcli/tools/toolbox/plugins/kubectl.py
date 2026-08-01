"""Curated plugin for kubectl (Kubernetes CLI). One binary, many subcommands."""

from __future__ import annotations

import re

from pcli.tools.toolbox.plugin_base import CommandSpec, DetectionResult, ToolboxPlugin

_VERSION_RE = re.compile(r"[Vv]ersion:\s*v?(\d+)\.(\d+)\.(\d+)")


def _get_args(args: dict) -> list[str]:
    argv = ["get", args["resource"]]
    if args.get("name"):
        argv.append(args["name"])
    if args.get("all_namespaces"):
        argv.append("--all-namespaces")
    elif args.get("namespace"):
        argv += ["--namespace", args["namespace"]]
    if args.get("output"):
        argv += ["-o", args["output"]]
    return argv


def _describe_args(args: dict) -> list[str]:
    argv = ["describe", args["resource"], args["name"]]
    if args.get("namespace"):
        argv += ["--namespace", args["namespace"]]
    return argv


def _logs_args(args: dict) -> list[str]:
    argv = ["logs", args["pod_name"]]
    if args.get("namespace"):
        argv += ["--namespace", args["namespace"]]
    if args.get("container"):
        argv += ["-c", args["container"]]
    if args.get("tail"):
        argv += ["--tail", str(args["tail"])]
    if args.get("previous"):
        argv.append("--previous")
    return argv


def _get_contexts_args(_args: dict) -> list[str]:
    return ["config", "get-contexts"]


def _apply_args(args: dict) -> list[str]:
    argv = ["apply", "-f", args["file"]]
    if args.get("namespace"):
        argv += ["--namespace", args["namespace"]]
    if args.get("dry_run"):
        argv.append("--dry-run=client")
    return argv


def _scale_args(args: dict) -> list[str]:
    argv = ["scale", args["resource"], args["name"], "--replicas", str(args["replicas"])]
    if args.get("namespace"):
        argv += ["--namespace", args["namespace"]]
    return argv


def _delete_args(args: dict) -> list[str]:
    argv = ["delete", args["resource"], args["name"]]
    if args.get("namespace"):
        argv += ["--namespace", args["namespace"]]
    return argv


class KubectlPlugin(ToolboxPlugin):
    software_name = "kubectl"
    binary_names = ("kubectl",)
    version_args = ("version", "--client")

    def parse_version(self, version_output: str) -> tuple[int, ...] | None:
        match = _VERSION_RE.search(version_output)
        if not match:
            return None
        return tuple(int(g) for g in match.groups())

    def build_tools(self, detection: DetectionResult) -> list[CommandSpec]:
        binary = detection.binary_path
        return [
            CommandSpec(
                name="get",
                description="List Kubernetes resources of a given type (pods, deployments, "
                "services, nodes, ...).",
                parameters={
                    "type": "object",
                    "properties": {
                        "resource": {
                            "type": "string",
                            "description": "Resource type, e.g. 'pods', 'deployments', 'svc', 'nodes'.",
                        },
                        "name": {"type": "string", "description": "Specific resource name (optional)."},
                        "namespace": {"type": "string"},
                        "all_namespaces": {"type": "boolean"},
                        "output": {
                            "type": "string",
                            "description": "Output format, e.g. 'json', 'yaml', 'wide'.",
                        },
                    },
                    "required": ["resource"],
                },
                build_args=_get_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="describe",
                description="Show detailed information about a specific Kubernetes resource.",
                parameters={
                    "type": "object",
                    "properties": {
                        "resource": {"type": "string"},
                        "name": {"type": "string"},
                        "namespace": {"type": "string"},
                    },
                    "required": ["resource", "name"],
                },
                build_args=_describe_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="logs",
                description="Fetch logs from a pod (optionally a specific container).",
                parameters={
                    "type": "object",
                    "properties": {
                        "pod_name": {"type": "string"},
                        "namespace": {"type": "string"},
                        "container": {"type": "string"},
                        "tail": {
                            "type": "integer",
                            "description": "Number of lines from the end of the log to show.",
                        },
                        "previous": {
                            "type": "boolean",
                            "description": "Show logs from the previous terminated container instance.",
                        },
                    },
                    "required": ["pod_name"],
                },
                build_args=_logs_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="get_contexts",
                description="List available kubeconfig contexts.",
                parameters={"type": "object", "properties": {}},
                build_args=_get_contexts_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="apply",
                description="Apply a Kubernetes manifest file (create/update resources).",
                parameters={
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Path to the manifest file."},
                        "namespace": {"type": "string"},
                        "dry_run": {
                            "type": "boolean",
                            "description": "Preview the change without applying it.",
                        },
                    },
                    "required": ["file"],
                },
                build_args=_apply_args,
                binary_path=binary,
                risk="mutate",
            ),
            CommandSpec(
                name="scale",
                description="Scale a deployment/replicaset/statefulset to a given replica count.",
                parameters={
                    "type": "object",
                    "properties": {
                        "resource": {"type": "string", "description": "e.g. 'deployment'."},
                        "name": {"type": "string"},
                        "replicas": {"type": "integer"},
                        "namespace": {"type": "string"},
                    },
                    "required": ["resource", "name", "replicas"],
                },
                build_args=_scale_args,
                binary_path=binary,
                risk="mutate",
            ),
            CommandSpec(
                name="delete",
                description="Delete a specific Kubernetes resource.",
                parameters={
                    "type": "object",
                    "properties": {
                        "resource": {"type": "string"},
                        "name": {"type": "string"},
                        "namespace": {"type": "string"},
                    },
                    "required": ["resource", "name"],
                },
                build_args=_delete_args,
                binary_path=binary,
                risk="destructive",
            ),
        ]
