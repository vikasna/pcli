"""ToolboxManager: user-triggered discovery of OS/software tools, persisted
across sessions and merged into the shared ToolRegistry on every startup.

Discovery is only ever invoked by discover() — nothing here runs
automatically or in the background."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pcli.llm.client import GatewayClient
from pcli.sandbox.base import ExecRequest, SandboxSecurityError
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.registry import ToolRegistry
from pcli.tools.toolbox import store
from pcli.tools.toolbox.introspect import collect_help_corpus
from pcli.tools.toolbox.plugin_base import CommandSpec, DetectionResult, ToolboxPlugin
from pcli.tools.toolbox.plugins import ALL_PLUGINS
from pcli.tools.toolbox.synthesize import synthesize_tools


class ToolboxDiscoveryError(Exception):
    pass


def _plugin_for(software_name: str) -> ToolboxPlugin | None:
    for plugin in ALL_PLUGINS:
        if plugin.software_name == software_name:
            return plugin
    return None


async def _detect_primary_binary(
    candidates: tuple[str, ...], version_args: tuple[str, ...], cwd: Path
) -> tuple[str, str] | None:
    for candidate in candidates:
        binary_path = shutil.which(candidate)
        if not binary_path:
            continue
        sandbox = RestrictedSubprocessSandbox(allowed_roots=[cwd])
        result = await sandbox.execute(
            ExecRequest(command=[binary_path, *version_args], cwd=cwd, timeout_s=10)
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return binary_path, output
    return None


def make_command_tool_spec(software_name: str, spec: CommandSpec) -> ToolSpec:
    tool_name = f"{software_name}_{spec.name}"

    async def _handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        argv = [spec.binary_path, *spec.build_args(arguments)]
        result = await ctx.sandbox.execute(ExecRequest(command=argv, cwd=ctx.cwd, timeout_s=30))
        output = result.stdout
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        is_error = result.exit_code != 0 or result.timed_out
        return ToolResult(output=f"[exit_code={result.exit_code}]\n{output}", is_error=is_error)

    return ToolSpec(
        name=tool_name,
        description=spec.description,
        parameters=spec.parameters,
        handler=_handler,
        needs_permission=spec.risk != "read",
        needs_sandbox=True,
        risk_description=f"Runs `{spec.binary_path}` ({spec.risk}).",
        read_only=spec.risk == "read",
    )


def _auto_flags(arguments: dict, parameters: dict) -> list[str]:
    """Generic best-effort flag builder for LLM-synthesized tools: each
    provided argument becomes `--arg-name value` (or a bare flag for
    booleans). Curated plugins use hand-written build_args instead because
    real CLIs (positional args, short flags, `=` syntax) don't uniformly fit
    this convention — this is the fallback for tools nobody's hand-reviewed."""
    props = parameters.get("properties", {})
    argv: list[str] = []
    for key, value in arguments.items():
        if value is None:
            continue
        flag = "--" + key.replace("_", "-")
        prop_type = (props.get(key) or {}).get("type")
        if prop_type == "boolean":
            if value:
                argv.append(flag)
            continue
        if isinstance(value, list):
            for item in value:
                argv += [flag, str(item)]
            continue
        argv += [flag, str(value)]
    return argv


def _invocation_for_path(path: Path) -> list[str]:
    """.py scripts need an interpreter — Windows can't execve them directly,
    and POSIX only can if they're chmod +x with a shebang, which a
    self-authored script can't be relied on to have."""
    if path.suffix.lower() == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def make_synthesized_tool_spec(software_name: str, invocation: list[str], tool_data: dict) -> ToolSpec:
    tool_name = f"{software_name}_{tool_data['name']}"
    subcommand: list[str] = tool_data["subcommand"]
    parameters: dict = tool_data["parameters"]
    risk: str = tool_data.get("risk", "mutate")

    async def _handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        argv = [*invocation, *subcommand, *_auto_flags(arguments, parameters)]
        result = await ctx.sandbox.execute(ExecRequest(command=argv, cwd=ctx.cwd, timeout_s=30))
        output = result.stdout
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        is_error = result.exit_code != 0 or result.timed_out
        return ToolResult(output=f"[exit_code={result.exit_code}]\n{output}", is_error=is_error)

    invocation_label = " ".join(invocation)
    return ToolSpec(
        name=tool_name,
        description=tool_data["description"]
        + " (auto-generated from --help output; unreviewed, verify before trusting blindly)",
        parameters=parameters,
        handler=_handler,
        needs_permission=risk != "read",
        needs_sandbox=True,
        risk_description=f"Runs `{invocation_label} {' '.join(subcommand)}` ({risk}, auto-generated).",
        read_only=risk == "read",
    )


class ToolboxManager:
    def __init__(self, *, cwd: Path) -> None:
        self._cwd = cwd

    def _resolve_script_path(self, path: str) -> Path:
        """Resolves+validates a self-authored script for discover's path=
        parameter: must land inside self._cwd (the same containment
        RestrictedSubprocessSandbox._validate_cwd enforces for every other
        tool), so a path argument can't reach outside the project tree the
        agent is actually working in."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._cwd / candidate
        resolved = candidate.resolve()
        cwd_resolved = self._cwd.expanduser().resolve()
        if resolved != cwd_resolved and cwd_resolved not in resolved.parents:
            raise SandboxSecurityError(
                f"'{path}' is outside the working directory '{cwd_resolved}'."
            )
        if not resolved.is_file():
            raise ToolboxDiscoveryError(f"'{path}' doesn't exist or isn't a file.")
        return resolved

    async def discover(
        self,
        software_name: str,
        *,
        gateway_client: GatewayClient | None = None,
        model: str | None = None,
        path: str | None = None,
    ) -> str:
        """path registers a self-authored script directly, bypassing PATH
        lookup entirely — for a model that just wrote its own small tool and
        wants to make it callable without the user placing it on PATH first.
        Curated-plugin matching is skipped when path is given: pointing at a
        specific script means "use exactly this," not "look something up.\""""
        registry = store.read_registry()

        if path is not None:
            script_path = self._resolve_script_path(path)
            invocation = _invocation_for_path(script_path)
            help_corpus = await collect_help_corpus(invocation, self._cwd)
            if not help_corpus.strip():
                raise ToolboxDiscoveryError(
                    f"'{' '.join(invocation)} --help' produced no output to work from."
                )
            corpus_hash = store.hash_corpus(help_corpus)

            cached = store.read_synthesized_schema(software_name)
            if cached and cached.get("help_corpus_hash") == corpus_hash:
                tools_data = cached["tools"]
            else:
                if gateway_client is None:
                    raise ToolboxDiscoveryError(
                        f"No LLM gateway configured to synthesize a tool schema for "
                        f"'{software_name}'."
                    )
                tools_data = await synthesize_tools(
                    gateway_client, software_name=software_name, help_corpus=help_corpus, model=model
                )
                store.write_synthesized_schema(
                    software_name, help_corpus_hash=corpus_hash, version="unknown", tools=tools_data
                )

            registry[software_name] = {
                "source": "synthesized",
                "binary_path": str(script_path),
                "invocation": invocation,
                "version": "unknown",
                "tool_count": len(tools_data),
            }
            store.write_registry(registry)
            return (
                f"Registered '{software_name}' from {script_path} — synthesized "
                f"{len(tools_data)} tool(s) from --help output."
            )

        plugin = _plugin_for(software_name)

        if plugin is not None:
            found = await _detect_primary_binary(plugin.binary_names, plugin.version_args, self._cwd)
            if found is None:
                raise ToolboxDiscoveryError(
                    f"Could not find '{software_name}' on PATH "
                    f"(tried: {', '.join(plugin.binary_names)})."
                )
            binary_path, version_output = found
            version = plugin.parse_version(version_output) or (0,)
            first_line = version_output.strip().splitlines()[0] if version_output.strip() else "unknown"
            detection = DetectionResult(
                software_name=software_name,
                binary_path=binary_path,
                version_string=first_line,
                version=version,
            )
            tools = plugin.build_tools(detection)
            registry[software_name] = {
                "source": "curated",
                "binary_path": binary_path,
                "version": detection.version_string,
                "tool_count": len(tools),
            }
            store.write_registry(registry)
            return (
                f"Found curated plugin for '{software_name}' at {binary_path} "
                f"(version: {detection.version_string}). Registered {len(tools)} tool(s)."
            )

        binary_path = shutil.which(software_name)
        if not binary_path:
            raise ToolboxDiscoveryError(f"'{software_name}' was not found on PATH.")

        help_corpus = await collect_help_corpus([binary_path], self._cwd)
        if not help_corpus.strip():
            raise ToolboxDiscoveryError(f"'{software_name} --help' produced no output to work from.")
        corpus_hash = store.hash_corpus(help_corpus)

        cached = store.read_synthesized_schema(software_name)
        if cached and cached.get("help_corpus_hash") == corpus_hash:
            tools_data = cached["tools"]
        else:
            if gateway_client is None:
                raise ToolboxDiscoveryError(
                    f"No curated plugin for '{software_name}' and no LLM gateway configured "
                    "to synthesize one."
                )
            tools_data = await synthesize_tools(
                gateway_client, software_name=software_name, help_corpus=help_corpus, model=model
            )
            store.write_synthesized_schema(
                software_name, help_corpus_hash=corpus_hash, version="unknown", tools=tools_data
            )

        registry[software_name] = {
            "source": "synthesized",
            "binary_path": binary_path,
            "invocation": [binary_path],
            "version": "unknown",
            "tool_count": len(tools_data),
        }
        store.write_registry(registry)
        return (
            f"No curated plugin for '{software_name}' — introspected {binary_path} and "
            f"synthesized {len(tools_data)} tool(s) from --help output."
        )

    def list_discovered(self) -> dict[str, dict]:
        return store.read_registry()

    def remove(self, software_name: str) -> None:
        registry = store.read_registry()
        registry.pop(software_name, None)
        store.write_registry(registry)

    async def load_all(self) -> ToolRegistry:
        """Rebuilds tool specs for every previously-discovered software.
        Curated entries re-detect live (cheap); synthesized entries load
        their cached schema. Missing binaries are skipped silently rather
        than raising, so a stale registry entry doesn't break startup."""
        registry = ToolRegistry()

        for software_name, entry in store.read_registry().items():
            plugin = _plugin_for(software_name)
            if plugin is not None:
                found = await _detect_primary_binary(plugin.binary_names, plugin.version_args, self._cwd)
                if found is None:
                    continue
                binary_path, version_output = found
                version = plugin.parse_version(version_output) or (0,)
                first_line = (
                    version_output.strip().splitlines()[0] if version_output.strip() else "unknown"
                )
                detection = DetectionResult(
                    software_name=software_name,
                    binary_path=binary_path,
                    version_string=first_line,
                    version=version,
                )
                for spec in plugin.build_tools(detection):
                    registry.register(make_command_tool_spec(software_name, spec))
            else:
                cached = store.read_synthesized_schema(software_name)
                if cached is None:
                    continue
                invocation = entry.get("invocation")
                if not invocation:
                    # Registry entries written before the invocation field
                    # existed only have binary_path - fall back to it.
                    binary_path = entry.get("binary_path") or shutil.which(software_name)
                    if not binary_path:
                        continue
                    invocation = [binary_path]
                for tool_data in cached["tools"]:
                    registry.register(
                        make_synthesized_tool_spec(software_name, invocation, tool_data)
                    )

        return registry
