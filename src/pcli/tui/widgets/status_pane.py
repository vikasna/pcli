"""Compact, fixed-height pane showing live todo-list status and any running
subagent's progress. Lives above the message history (not part of it) so it
doesn't scroll away, and collapses to nothing when there's nothing to show."""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from pcli.session.models import TodoItem

_PANE_HEIGHT = 3
_STATUS_ICONS = {"pending": "○", "in_progress": "▶", "completed": "✓"}
_STATUS_STYLES = {"pending": "", "in_progress": "bold", "completed": "dim"}
_TODO_PRIORITY = {"in_progress": 0, "pending": 1, "completed": 2}


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _todo_lines(todos: list[TodoItem], limit: int) -> list[Text]:
    if limit <= 0 or not todos:
        return []
    done = sum(1 for t in todos if t.status == "completed")
    ordered = sorted(todos, key=lambda t: _TODO_PRIORITY.get(t.status, 3))
    visible = ordered[:limit]
    hidden = len(todos) - len(visible)
    lines = [
        Text(
            f"{_STATUS_ICONS.get(t.status, '?')} {_truncate(t.content, 76)}",
            style=_STATUS_STYLES.get(t.status, ""),
        )
        for t in visible
    ]
    if hidden > 0:
        lines[-1] = Text(f"… +{hidden} more todo(s)  ({done}/{len(todos)} done)", style="dim")
    return lines


class StatusPane(Static):
    todos: reactive[list[TodoItem]] = reactive(list)
    subagent_task: reactive[str | None] = reactive(None)
    subagent_tool_calls: reactive[int] = reactive(0)
    subagent_last_tool: reactive[str | None] = reactive(None)

    def on_mount(self) -> None:
        self._sync_visibility()

    def render(self) -> Group | str:
        lines: list[Text] = []

        if self.subagent_task is not None:
            detail = f", last: {self.subagent_last_tool}" if self.subagent_last_tool else ""
            lines.append(
                Text(
                    f"⟳ Subagent: {_truncate(self.subagent_task, 60)} "
                    f"— {self.subagent_tool_calls} tool call(s){detail}",
                    style="italic",
                )
            )

        lines.extend(_todo_lines(self.todos, _PANE_HEIGHT - len(lines)))

        if not lines:
            return ""
        return Group(*lines)

    def watch_todos(self, _todos: list[TodoItem]) -> None:
        self._sync_visibility()

    def watch_subagent_task(self, _task: str | None) -> None:
        self._sync_visibility()

    def _sync_visibility(self) -> None:
        has_content = bool(self.todos) or self.subagent_task is not None
        self.styles.display = "block" if has_content else "none"
