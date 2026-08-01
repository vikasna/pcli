"""Curated plugin for Apache Kafka's admin CLIs (kafka-topics,
kafka-consumer-groups). Like SGE, this is a suite of separate scripts, so
sibling binaries are resolved independently in build_tools()."""

from __future__ import annotations

import re
import shutil

from pcli.tools.toolbox.plugin_base import CommandSpec, DetectionResult, ToolboxPlugin

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_BOOTSTRAP_PARAM = {
    "bootstrap_server": {
        "type": "string",
        "description": "Kafka bootstrap server, e.g. 'localhost:9092'.",
    }
}


def _which_any(*names: str) -> str | None:
    for name in names:
        if path := shutil.which(name):
            return path
    return None


def _list_topics_args(args: dict) -> list[str]:
    return ["--bootstrap-server", args["bootstrap_server"], "--list"]


def _describe_topic_args(args: dict) -> list[str]:
    return ["--bootstrap-server", args["bootstrap_server"], "--describe", "--topic", args["topic"]]


def _create_topic_args(args: dict) -> list[str]:
    return [
        "--bootstrap-server",
        args["bootstrap_server"],
        "--create",
        "--topic",
        args["topic"],
        "--partitions",
        str(args.get("partitions", 1)),
        "--replication-factor",
        str(args.get("replication_factor", 1)),
    ]


def _delete_topic_args(args: dict) -> list[str]:
    return ["--bootstrap-server", args["bootstrap_server"], "--delete", "--topic", args["topic"]]


def _list_consumer_groups_args(args: dict) -> list[str]:
    return ["--bootstrap-server", args["bootstrap_server"], "--list"]


def _describe_consumer_group_args(args: dict) -> list[str]:
    return ["--bootstrap-server", args["bootstrap_server"], "--describe", "--group", args["group"]]


class KafkaPlugin(ToolboxPlugin):
    software_name = "kafka"
    binary_names = ("kafka-topics.sh", "kafka-topics", "kafka-topics.bat")
    version_args = ("--version",)

    def parse_version(self, version_output: str) -> tuple[int, ...] | None:
        match = _VERSION_RE.search(version_output)
        if not match:
            return None
        return tuple(int(g) for g in match.groups())

    def build_tools(self, detection: DetectionResult) -> list[CommandSpec]:
        topics_bin = detection.binary_path
        specs = [
            CommandSpec(
                name="list_topics",
                description="List all Kafka topics.",
                parameters={
                    "type": "object",
                    "properties": _BOOTSTRAP_PARAM,
                    "required": ["bootstrap_server"],
                },
                build_args=_list_topics_args,
                binary_path=topics_bin,
                risk="read",
            ),
            CommandSpec(
                name="describe_topic",
                description="Show partition/replica detail for a specific topic.",
                parameters={
                    "type": "object",
                    "properties": {**_BOOTSTRAP_PARAM, "topic": {"type": "string"}},
                    "required": ["bootstrap_server", "topic"],
                },
                build_args=_describe_topic_args,
                binary_path=topics_bin,
                risk="read",
            ),
            CommandSpec(
                name="create_topic",
                description="Create a new Kafka topic.",
                parameters={
                    "type": "object",
                    "properties": {
                        **_BOOTSTRAP_PARAM,
                        "topic": {"type": "string"},
                        "partitions": {"type": "integer"},
                        "replication_factor": {"type": "integer"},
                    },
                    "required": ["bootstrap_server", "topic"],
                },
                build_args=_create_topic_args,
                binary_path=topics_bin,
                risk="mutate",
            ),
            CommandSpec(
                name="delete_topic",
                description="Delete a Kafka topic.",
                parameters={
                    "type": "object",
                    "properties": {**_BOOTSTRAP_PARAM, "topic": {"type": "string"}},
                    "required": ["bootstrap_server", "topic"],
                },
                build_args=_delete_topic_args,
                binary_path=topics_bin,
                risk="destructive",
            ),
        ]

        if consumer_groups_bin := _which_any(
            "kafka-consumer-groups.sh", "kafka-consumer-groups", "kafka-consumer-groups.bat"
        ):
            specs += [
                CommandSpec(
                    name="list_consumer_groups",
                    description="List Kafka consumer groups.",
                    parameters={
                        "type": "object",
                        "properties": _BOOTSTRAP_PARAM,
                        "required": ["bootstrap_server"],
                    },
                    build_args=_list_consumer_groups_args,
                    binary_path=consumer_groups_bin,
                    risk="read",
                ),
                CommandSpec(
                    name="describe_consumer_group",
                    description="Show lag/offset detail for a specific consumer group.",
                    parameters={
                        "type": "object",
                        "properties": {**_BOOTSTRAP_PARAM, "group": {"type": "string"}},
                        "required": ["bootstrap_server", "group"],
                    },
                    build_args=_describe_consumer_group_args,
                    binary_path=consumer_groups_bin,
                    risk="read",
                ),
            ]

        return specs
