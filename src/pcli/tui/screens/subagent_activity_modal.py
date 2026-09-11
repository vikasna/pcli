"""SubagentActivityModal: Ctrl+G's live-updating view of the current
subagent's task and full tool-call history — the same content /subagent
prints as a one-off snapshot (format_subagent_activity), but reactive:
subscribes to ActivityTracker on mount and re-renders on every change while
open, so watching a subagent work doesn't mean repeatedly re-typing
/subagent for a fresh snapshot.

Read-only by design: if the subagent is blocked on ask_user_question, the
question is shown here for context, but answering it still happens through
the existing AskQuestionModal (ask_question_via_modal) that ask_tool.py
already pops up automatically — Textual's modal stack handles both being
open without conflict, no special-casing needed here.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from pcli.agent.activity import ActivityTracker, format_subagent_activity


class SubagentActivityModal(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]

    def __init__(self, activity: ActivityTracker) -> None:
        super().__init__()
        self._activity = activity

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="subagent-activity-modal"):
            yield Static(id="subagent-activity-body")

    def on_mount(self) -> None:
        self._refresh()
        self._activity.subscribe(self._refresh)

    def on_unmount(self) -> None:
        self._activity.unsubscribe(self._refresh)

    def _refresh(self) -> None:
        sub = self._activity.subagent
        body = "Subagent finished — nothing more to show." if sub is None else format_subagent_activity(sub)
        self.query_one("#subagent-activity-body", Static).update(f"{body}\n\n[Esc] Close")
        self.query_one(VerticalScroll).scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)
