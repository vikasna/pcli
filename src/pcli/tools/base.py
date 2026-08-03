"""Tool definitions: what the LLM can call, and how a call actually runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pcli.agent.activity import ActivityTracker
from pcli.llm.client import GatewayClient
from pcli.llm.models import ToolDefinition, Usage
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.sandbox.base import Sandbox
from pcli.session.models import Session
from pcli.tools.artifacts import ArtifactStore

if TYPE_CHECKING:
    from pcli.tools.registry import ToolRegistry


@dataclass
class ToolContext:
    sandbox: Sandbox
    guardrails: GuardrailsConfig
    cwd: Path
    # The rest are only populated for the top-level agent loop's context; most
    # tool handlers never touch them. They exist so a tool (namely
    # spawn_subagent) can construct its own nested AgentLoop that shares the
    # parent's gateway/tools/permissions instead of re-plumbing all of this
    # through a parallel context type.
    gateway_client: GatewayClient | None = None
    model: str | None = None
    tool_registry: ToolRegistry | None = None
    permission_manager: PermissionManager | None = None
    ask: AskCallback | None = None
    max_tool_iterations: int = 25
    subagent_depth: int = 0
    session: Session | None = None
    """The live Session object, for tools that read/mutate session-level state
    directly (namely write_todos)."""
    artifact_store: ArtifactStore | None = None
    """Where large tool outputs get archived (see agent/loop.py's automatic
    truncation) and where fetch_artifact reads them back from."""
    activity: ActivityTracker | None = None
    """Ephemeral live-progress reporting for the TUI's status pane (namely
    spawn_subagent reporting its own tool-call progress). Not persisted."""


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    extra_usage: list[Usage] = field(default_factory=list)
    """LLM usage incurred by the tool call itself (e.g. a subagent's own LLM
    calls) that didn't come from the turn's main chat_stream — the caller
    folds this into cost tracking so subagent spend isn't silently dropped."""


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    """JSON schema for the tool's arguments (the OpenAI-style `function.parameters`)."""
    handler: ToolHandler
    needs_permission: bool = True
    needs_sandbox: bool = False
    risk_description: str = ""
    guardrail_command_arg: str | None = None
    """Name of the argument holding a shell command, if any — checked against
    the shell denylist regardless of needs_permission."""
    guardrail_path_arg: str | None = None
    """Name of the argument holding a filesystem path, if any — checked
    against allowed_roots/deny_paths regardless of needs_permission."""
    guardrail_python_module_arg: str | None = None
    """Name of the argument holding a (possibly dotted) Python module/qualified
    name, if any — its top-level module is checked against the module denylist
    regardless of needs_permission."""

    def to_openai_tool(self) -> ToolDefinition:
        return ToolDefinition(
            function={
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        )
