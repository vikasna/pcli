"""Curated plugin for Apache httpd, via apachectl/apache2ctl."""

from __future__ import annotations

import re

from pcli.tools.toolbox.plugin_base import CommandSpec, DetectionResult, ToolboxPlugin

_VERSION_RE = re.compile(r"Apache/(\d+)\.(\d+)\.(\d+)")


def _configtest_args(_args: dict) -> list[str]:
    return ["-t"]


def _dump_vhosts_args(_args: dict) -> list[str]:
    return ["-S"]


def _dump_modules_args(_args: dict) -> list[str]:
    return ["-M"]


def _graceful_args(_args: dict) -> list[str]:
    return ["-k", "graceful"]


def _restart_args(_args: dict) -> list[str]:
    return ["-k", "restart"]


def _stop_args(_args: dict) -> list[str]:
    return ["-k", "stop"]


class HttpdPlugin(ToolboxPlugin):
    software_name = "httpd"
    binary_names = ("apachectl", "apache2ctl", "httpd", "apache2")
    version_args = ("-v",)

    def parse_version(self, version_output: str) -> tuple[int, ...] | None:
        match = _VERSION_RE.search(version_output)
        if not match:
            return None
        return tuple(int(g) for g in match.groups())

    def build_tools(self, detection: DetectionResult) -> list[CommandSpec]:
        binary = detection.binary_path
        return [
            CommandSpec(
                name="configtest",
                description="Check the Apache httpd configuration for syntax errors.",
                parameters={"type": "object", "properties": {}},
                build_args=_configtest_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="dump_vhosts",
                description="Show the parsed virtual host configuration.",
                parameters={"type": "object", "properties": {}},
                build_args=_dump_vhosts_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="dump_modules",
                description="List loaded Apache modules.",
                parameters={"type": "object", "properties": {}},
                build_args=_dump_modules_args,
                binary_path=binary,
                risk="read",
            ),
            CommandSpec(
                name="graceful_restart",
                description="Gracefully restart httpd (reload config without dropping "
                "existing connections).",
                parameters={"type": "object", "properties": {}},
                build_args=_graceful_args,
                binary_path=binary,
                risk="mutate",
            ),
            CommandSpec(
                name="restart",
                description="Restart httpd.",
                parameters={"type": "object", "properties": {}},
                build_args=_restart_args,
                binary_path=binary,
                risk="mutate",
            ),
            CommandSpec(
                name="stop",
                description="Stop httpd.",
                parameters={"type": "object", "properties": {}},
                build_args=_stop_args,
                binary_path=binary,
                risk="destructive",
            ),
        ]
