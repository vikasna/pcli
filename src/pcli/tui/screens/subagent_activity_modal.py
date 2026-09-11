"""SubagentActivityModal: Ctrl+G's live-updating, full-screen view of the
current subagent's task and full tool-call history — the same content
/subagent prints as a one-off snapshot (format_subagent_activity), but
reactive: subscribes to ActivityTracker on mount and re-renders on every
change while open, so watching a subagent work doesn't mean repeatedly
re-typing /subagent for a fresh snapshot.

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
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from pcli.agent.activity import ActivityTracker, format_subagent_activity


class SubagentActivityModal(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Return to main window")]

    def __init__(self, activity: ActivityTracker) -> None:
        super().__init__()
        self._activity = activity

    def compose(self) -> ComposeResult:
        with Vertical(id="subagent-activity-modal"):
            yield Static(
                "Subagent activity — press Esc to return to the main agent's window",
                id="subagent-activity-title",
            )
            with VerticalScroll(id="subagent-activity-scroll"):
                yield Static(id="subagent-activity-body")
            yield Button("Close", id="subagent-activity-close-button")

    def on_mount(self) -> None:
        self._refresh()
        self._activity.subscribe(self._refresh)

    def on_unmount(self) -> None:
        self._activity.unsubscribe(self._refresh)

    def _refresh(self) -> None:
        sub = self._activity.subagent
        body = "Subagent finished — nothing more to show." if sub is None else format_subagent_activity(sub)
        self.query_one("#subagent-activity-body", Static).update(body)
        self.query_one("#subagent-activity-scroll", VerticalScroll).scroll_end(animate=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "subagent-activity-close-button":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
