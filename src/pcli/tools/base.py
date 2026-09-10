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
    from pcli.tools.toolbox.manager import ToolboxManager


AskQuestionCallback = Callable[[str, list[str] | None], Awaitable[str]]


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
    ask_question: AskQuestionCallback | None = None
    """Callback for ask_user_question (tools/builtin/ask_tool.py) to pause a
    turn and ask the user something directly - see AskQuestionModal. Kept
    separate from `ask` (permission decisions only, a fixed allow/deny/
    remember-scope shape) since this is a free-form question/answer, not a
    permission choice."""
    max_tool_iterations: int | None = 25
    """None means unlimited (local-api mode)."""
    subagent_max_iterations: int = 30
    """Hard ceiling on a nested subagent's own tool-call iterations (see
    Settings.subagent_max_iterations) - unlike max_tool_iterations above,
    this is never None/unlimited, even in local-api mode."""
    max_response_tokens: int | None = None
    """The parent AgentLoop's current dynamic max_tokens cap (cost/context.py's
    compute_max_response_tokens), threaded through so a nested AgentLoop
    (spawn_subagent, agent_tools.py's make_agent_tool) inherits it instead of
    running with no cap at all — see AgentLoop.max_response_tokens."""
    temperature: float | None = None
    """The parent AgentLoop's current sampling temperature (Settings.
    default_temperature via /temperature), threaded through for the same
    subagent-inheritance reason as max_response_tokens above."""
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
    toolbox_manager: ToolboxManager | None = None
    """Lets register_toolbox_tool trigger toolbox discovery directly,
    mirroring what the /toolbox discover slash command does."""
    plan_mode: bool = False
    """True while the session is in plan mode — checked by AgentLoop as a
    dispatch-time backstop (see _dispatch_tool_call) independent of whatever
    registry the caller happened to build, and by spawn_subagent to keep a
    nested subagent from being used as a plan-mode bypass."""
    brave_search_api_key: str = ""
    """web_search (tools/builtin/web_tools.py) uses this real, supported API
    when configured (non-empty); otherwise it falls back to a best-effort,
    no-API-key scrape of DuckDuckGo's HTML results page."""


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
    plan_mode_safe: bool = False
    """Explicit opt-in for use while plan mode is active. Deliberately NOT
    derived from needs_permission — needs_permission=False is not an accurate
    read-only proxy (e.g. write_todos/record_decision mutate session state
    but don't need permission)."""

    def to_openai_tool(self) -> ToolDefinition:
        """The advertised schema gets an extra, optional "purpose" property
        injected on top of self.parameters — a shallow copy, never mutating
        self.parameters itself, which stays the schema AgentLoop validates
        real arguments against (see _dispatch_tool_call, which pops
        "purpose" out before that validation and before the tool handler
        ever sees it — this property exists purely for the model's benefit,
        not as a real tool argument)."""
        parameters = self.parameters
        if "properties" in parameters:
            parameters = {
                **parameters,
                "properties": {
                    **parameters["properties"],
                    "purpose": {
                        "type": "string",
                        "description": "Optional: a short, one-sentence reason you're calling "
                        "this tool right now (e.g. 'checking whether pdftotext is installed'). "
                        "Helps keep tool-call history readable and prunable.",
                    },
                },
            }
        return ToolDefinition(
            function={
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            }
        )
