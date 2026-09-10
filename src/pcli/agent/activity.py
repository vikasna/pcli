"""Ephemeral live-activity state for the TUI's status pane.

Deliberately NOT part of Session (not persisted, not exported/imported): it
exists only so a running subagent can report its progress up to the screen
without cluttering the main conversation with its intermediate tool calls
(see tools/builtin/subagent_tool.py's own docstring on that design choice).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SubagentToolCall:
    name: str
    arguments: str


@dataclass
class SubagentActivity:
    task: str
    tool_calls: int = 0
    last_tool: str | None = None
    call_log: list[SubagentToolCall] = field(default_factory=list)
    """Full history of tool calls made so far (name + raw arguments JSON),
    for the /subagent inspect command and for giving the user something to
    relate an in-flight ask_user_question to - tool_calls/last_tool above
    stay as plain counters/names for the existing status-bar line."""
    pending_question: tuple[str, list[str] | None] | None = None
    """Set while the subagent is blocked inside ask_user_question, so /subagent
    can show what it's currently waiting on."""


class ActivityTracker:
    """One shared instance per chat screen. A single subagent slot is enough
    since the agent loop dispatches tool calls sequentially, never
    concurrently — at most one subagent is ever running at a time."""

    def __init__(self) -> None:
        self._subagent: SubagentActivity | None = None
        self._on_change: Callable[[], None] | None = None

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._on_change = callback

    @property
    def subagent(self) -> SubagentActivity | None:
        return self._subagent

    def start_subagent(self, task: str) -> None:
        self._subagent = SubagentActivity(task=task)
        self._notify()

    def record_subagent_tool_call(self, tool_name: str, arguments: str = "") -> None:
        if self._subagent is None:
            return
        self._subagent.tool_calls += 1
        self._subagent.last_tool = tool_name
        self._subagent.call_log.append(SubagentToolCall(name=tool_name, arguments=arguments))
        self._notify()

    def set_subagent_pending_question(self, question: str, options: list[str] | None) -> None:
        if self._subagent is None:
            return
        self._subagent.pending_question = (question, options)
        self._notify()

    def clear_subagent_pending_question(self) -> None:
        if self._subagent is None:
            return
        self._subagent.pending_question = None
        self._notify()

    def finish_subagent(self) -> None:
        self._subagent = None
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()
