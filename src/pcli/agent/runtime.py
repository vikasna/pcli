"""Builds the UI-agnostic pieces of a working agent: sandbox, tool registry
(builtins + toolbox + persisted agent tools, with the local-api-only
ask_artifact filter), and a configured GatewayClient - everything
ChatScreen.on_mount (tui/screens/chat.py) already assembles at startup,
minus its message_view progress notices and context-limit auto-detection
(both genuinely TUI-specific - see build_agent_runtime's own docstring).

Shared by ChatScreen (the interactive TUI), `pcli run` (one-shot headless
execution), and `pcli telegram` (the Telegram daemon) - three different
front ends driving the identical AgentLoop machinery underneath. Each
caller still constructs its own AgentLoop from the returned pieces (needs
its own tool_context_factory closure, which in turn needs a Session/ask
callback that doesn't exist until the caller has one) - see AgentRuntime's
own docstring for why that one step isn't folded in here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pcli.browser.session import BrowserSession
from pcli.config.settings import Settings
from pcli.llm.client import GatewayClient
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.sandbox.base import Sandbox
from pcli.sandbox.selector import select_sandbox
from pcli.session.models import Session
from pcli.tools.agent_tools_store import load_persisted_agent_tools
from pcli.tools.artifacts import ArtifactStore
from pcli.tools.base import AskQuestionCallback, ToolContext
from pcli.tools.builtin.artifact_tool import ASK_ARTIFACT
from pcli.tools.registry import ToolRegistry, build_default_registry
from pcli.tools.toolbox.manager import ToolboxManager


@dataclass
class AgentRuntime:
    sandbox: Sandbox
    tool_registry: ToolRegistry
    toolbox_manager: ToolboxManager
    toolbox_tools_loaded: int
    agent_tools_loaded: int
    client: GatewayClient
    """Deliberately does NOT include an AgentLoop: that needs a
    tool_context_factory closure, which needs a Session and an ask/
    ask_question callback pair - none of which exist yet at this point, and
    differ per caller (ChatScreen's own bound method vs. a plain headless/
    Telegram closure). Callers construct their own AgentLoop from
    tool_registry/client here plus effective_max_tool_iterations(settings)
    below - a few lines, not worth a factory-of-a-factory to avoid."""
    browser_session: BrowserSession
    """One Playwright wrapper (browser/session.py) shared for the runtime's
    whole life, threaded into every ToolContext built from it - see
    make_tool_context below. Cheap to hold even if never used: the actual
    browser process only launches on a browser_* tool's first real call
    (BrowserSession._ensure_page), and Playwright itself is only imported
    at that point too, so building one here costs nothing when the
    optional "browser" extra isn't installed and no browser tool is ever
    called. Callers are responsible for calling .close() on shutdown (see
    ChatScreen.on_unmount / `pcli run`'s finally block) - AgentRuntime
    itself has no lifecycle hook of its own."""


def effective_max_tool_iterations(settings: Settings) -> int | None:
    """None means unlimited - local-api mode. Shared so every AgentLoop
    construction site (ChatScreen, pcli run, pcli telegram) computes this
    identically; ChatScreen._effective_max_tool_iterations delegates here."""
    return None if settings.is_local_api() else settings.max_tool_iterations


def build_permission_manager(settings: Settings) -> PermissionManager:
    """GuardrailsConfig.load() (guardrails.toml) plus the local-api override
    that uncaps turn/rate limiting only - the security guardrails (shell
    denylist, fs roots, module denylist) are never touched by local-api
    mode, only the two rate-limiting fields. Shared so ChatScreen, `pcli
    run`, and `pcli telegram` all construct this identically."""
    guardrails = GuardrailsConfig.load()
    if settings.is_local_api():
        guardrails = guardrails.model_copy(
            update={"max_tool_calls_per_turn": 0, "max_tool_calls_per_minute": 0}
        )
    return PermissionManager(guardrails=guardrails)


async def build_agent_runtime(
    settings: Settings, cwd: Path, *, browser_headless: bool = False
) -> AgentRuntime:
    """Raises whatever select_sandbox raises (a sandbox backend failing to
    start) - callers decide how to report that themselves (ChatScreen shows
    a message_view notice and aborts startup; `pcli run`/`pcli telegram`
    print an error and exit non-zero), which is exactly why this doesn't
    swallow it internally.

    Skips context-limit auto-detection on purpose (see chat.py's own
    _maybe_detect_context_limit) - it's read-only, best-effort, and tied to
    TUI-specific retry state (_context_limit_retry_pending) that only makes
    sense across a live session's own turns. A headless/Telegram run simply
    uses whatever ContextLimitTable already has (a built-in default or an
    earlier /context-limit correction) - correctness is unaffected, only
    the auto-compaction threshold's assumed denominator could be off for a
    model nothing has ever probed or been told about.

    browser_headless defaults to False (a real, visible browser window) -
    the right default for an interactive TUI session, where seeing the
    browser work is part of what makes it trustworthy to watch. `pcli run`
    passes True by default instead (nothing to show, and a scheduled run
    shouldn't pop up a window), overridable with --headed."""
    sandbox = await select_sandbox(
        backend_override=settings.sandbox_backend,
        allowed_roots=[cwd],
        cpu_limit_s=settings.sandbox_cpu_limit_s,
        memory_limit_bytes=settings.sandbox_memory_limit_bytes,
    )

    tool_registry = build_default_registry()
    if not settings.is_local_api():
        # ask_artifact spends an extra LLM call answering a question about a
        # large artifact instead of returning raw content - free on a local
        # gateway (the whole point), a real if usually small cost on a paid
        # one, so it's simply not offered there rather than left to the
        # model's judgment to avoid using it.
        tool_registry = tool_registry.filtered(lambda t: t.name != ASK_ARTIFACT.name)

    toolbox_manager = ToolboxManager(cwd=cwd)
    toolbox_tools = await toolbox_manager.load_all()
    tool_registry.merge(toolbox_tools)

    agent_tools = load_persisted_agent_tools()
    tool_registry.merge(agent_tools)

    return AgentRuntime(
        sandbox=sandbox,
        tool_registry=tool_registry,
        toolbox_manager=toolbox_manager,
        toolbox_tools_loaded=len(toolbox_tools),
        agent_tools_loaded=len(agent_tools),
        client=GatewayClient(settings),
        browser_session=BrowserSession(headless=browser_headless),
    )


def make_tool_context(
    runtime: AgentRuntime,
    settings: Settings,
    cwd: Path,
    *,
    session: Session,
    permission_manager: PermissionManager,
    artifact_store: ArtifactStore | None = None,
    ask: AskCallback | None = None,
    ask_question: AskQuestionCallback | None = None,
    plan_mode: bool = False,
) -> ToolContext:
    """Same construction ChatScreen._make_tool_context does, generalized for
    any front end - `ask`/`ask_question` default to None (no UI to ask
    through), which permissions/manager.py and tools/builtin/ask_tool.py
    both already handle safely (fail-closed / "state your assumption and
    proceed" respectively - see agent/runtime.py's own module docstring for
    why that's the right headless default, not a gap). `activity` and
    `toolbox_manager` are left at their ToolContext defaults (None /
    unset-until-passed) for a headless caller - no live TUI status pane to
    report subagent progress into, and toolbox_manager is only needed for
    register_toolbox_tool's own discovery trigger, threaded through by
    callers that want it rather than assumed here."""
    return ToolContext(
        sandbox=runtime.sandbox,
        guardrails=permission_manager.guardrails,
        cwd=cwd,
        gateway_client=runtime.client,
        model=settings.default_model or None,
        tool_registry=runtime.tool_registry,
        permission_manager=permission_manager,
        ask=ask,
        ask_question=ask_question,
        brave_search_api_key=settings.brave_search_api_key,
        memory_enabled=settings.memory_enabled,
        memory_max_entries=settings.memory_max_entries,
        max_tool_iterations=effective_max_tool_iterations(settings),
        subagent_max_iterations=settings.subagent_max_iterations,
        session=session,
        max_session_cost_usd=settings.max_session_cost_usd,
        artifact_store=artifact_store,
        toolbox_manager=runtime.toolbox_manager,
        plan_mode=plan_mode,
        browser_session=runtime.browser_session,
    )
