"""Runs a single task non-interactively - the machinery behind `pcli run`
(cli.py) and each incoming message `pcli telegram` handles (telegram/
daemon.py). No MessageView/StatusBar: progress is reported through a plain
callback instead. `ask`/`ask_question` both default to None - no UI to ask
through - which the permission/ask_user_question machinery already handles
safely on its own (see agent/runtime.py's make_tool_context); `pcli
telegram` is the one caller that actually supplies them, wired to Telegram
inline-keyboard prompts instead of None.

Deliberately narrower than ChatScreen._run_one_turn: no tool-result
pruning, no auto-compaction, and no memory-extraction pass (all three are
tied to ChatScreen's own StatusBar/MessageView plumbing and are aimed at a
long-lived interactive session accumulating history over hours - a
headless run is normally short-lived per invocation). It does replicate
one piece: auto-continuing a response truncated by the token limit
(TurnCompleteEvent.response_truncated, see agent/loop.py) - skipping that
would be a *worse* silent-stall bug here than in the TUI, since there's no
user present to notice and type "continue" themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pcli.agent.loop import AgentLoop
from pcli.agent.prompt import build_system_prompt
from pcli.agent.runtime import AgentRuntime, effective_max_tool_iterations, make_tool_context
from pcli.config.settings import Settings
from pcli.cost.tracker import CostTracker, cost_budget_reason
from pcli.memory.models import render_memory_section
from pcli.memory.store import read_memory
from pcli.permissions.manager import AskCallback, PermissionManager
from pcli.session.models import Message, Session
from pcli.session.store import SessionStore
from pcli.tools.artifacts import SessionArtifactStore
from pcli.tools.base import AskQuestionCallback

_MAX_CONSECUTIVE_AUTO_CONTINUES = 3
"""Same cap and reasoning as ChatScreen's own _MAX_CONSECUTIVE_AUTO_CONTINUES
(tui/screens/chat.py) - not shared as a single constant, since the two
call sites' own docstrings are the more useful place to read the reasoning
from, and a numeric constant this small isn't worth an import just to avoid
repeating "3"."""
_AUTO_CONTINUE_MESSAGE = "Continue."

ProgressCallback = Callable[[str], None]


@dataclass
class HeadlessTurnResult:
    session: Session
    final_text: str
    terminated_early: bool
    truncations_exhausted: bool = False
    """True if the response was still being cut off by the token limit
    after _MAX_CONSECUTIVE_AUTO_CONTINUES automatic retries - the caller
    may want to flag this distinctly from an ordinary finish (e.g. a
    non-zero exit code from `pcli run`, since the work is likely genuinely
    incomplete)."""


def new_headless_session(
    store: SessionStore, settings: Settings, cwd: Path
) -> Session:
    """A fresh Session with the same system prompt (incl. global user
    memory, if enabled) a brand-new TUI session gets - see ChatScreen.
    __init__'s identical construction in tui/screens/chat.py."""
    extra_sections: list[str] = []
    if settings.memory_enabled:
        memory_section = render_memory_section(read_memory().entries)
        if memory_section:
            extra_sections.append(memory_section)
    session = store.new_session(
        model=settings.default_model,
        gateway_base_url=settings.gateway_base_url,
        working_dir=str(cwd),
    )
    session.messages.append(
        Message(role="system", content=build_system_prompt(extra_sections=extra_sections or None))
    )
    return session


async def run_headless_task(
    task: str,
    *,
    session: Session,
    runtime: AgentRuntime,
    settings: Settings,
    permission_manager: PermissionManager,
    cwd: Path,
    store: SessionStore,
    on_progress: ProgressCallback = lambda _line: None,
    ask: AskCallback | None = None,
    ask_question: AskQuestionCallback | None = None,
) -> HeadlessTurnResult:
    artifact_store = SessionArtifactStore(store, session.id)
    cost_tracker = CostTracker(session)

    def tool_context_factory():
        return make_tool_context(
            runtime,
            settings,
            cwd,
            session=session,
            permission_manager=permission_manager,
            artifact_store=artifact_store,
            ask=ask,
            ask_question=ask_question,
        )

    agent_loop = AgentLoop(
        runtime.client,
        model=settings.default_model or None,
        tool_registry=runtime.tool_registry,
        permission_manager=permission_manager,
        tool_context_factory=tool_context_factory,
        max_tool_iterations=effective_max_tool_iterations(settings),
        artifact_threshold_chars=settings.artifact_threshold_chars,
        temperature=settings.default_temperature,
    )

    session.messages.append(Message(role="user", content=task))
    on_progress(f"> {task}")
    store.save(session)

    final_text = ""
    terminated_early = False
    truncations_exhausted = False
    consecutive_truncations = 0
    run_again = True
    while run_again:
        run_again = False
        chat_messages = [m.to_chat_message() for m in session.messages]
        text_parts: list[str] = []
        async for event in agent_loop.run_turn(
            chat_messages,
            ask=ask,
            budget_check=lambda: cost_budget_reason(session, settings.max_session_cost_usd),
        ):
            if event.kind == "text_delta":
                text_parts.append(event.text)
            elif event.kind == "usage":
                cost_tracker.record_turn(settings.default_model or session.model, event.usage)
            elif event.kind == "tool_start":
                on_progress(
                    f"  -> {event.tool_call.function.name}({event.tool_call.function.arguments})"
                )
            elif event.kind == "tool_result":
                for extra in event.extra_usage:
                    cost_tracker.record_turn(
                        settings.default_model or session.model, extra, source="subagent"
                    )
                preview = event.output if len(event.output) <= 200 else event.output[:200] + "..."
                on_progress(f"  <- {preview}")
            elif event.kind == "turn_complete":
                session.messages.extend(Message.from_chat_message(m) for m in event.new_messages)
                terminated_early = event.terminated_early
                if event.response_truncated:
                    if consecutive_truncations < _MAX_CONSECUTIVE_AUTO_CONTINUES:
                        consecutive_truncations += 1
                        on_progress(
                            "  [pcli] Response was cut off by the token limit - continuing "
                            f"automatically ({consecutive_truncations}/{_MAX_CONSECUTIVE_AUTO_CONTINUES})."
                        )
                        session.messages.append(Message(role="user", content=_AUTO_CONTINUE_MESSAGE))
                        run_again = True
                    else:
                        truncations_exhausted = True
                else:
                    consecutive_truncations = 0
        store.save(session)
        final_text = "".join(text_parts) or final_text

    return HeadlessTurnResult(
        session=session,
        final_text=final_text,
        terminated_early=terminated_early,
        truncations_exhausted=truncations_exhausted,
    )
