"""Scrollable pane showing the live todo list. Lives above the message
history (not part of it) so it doesn't scroll away with the conversation.
4 lines of todos visible by default (#status-pane's height in pcli.tcss is
5 — 4 content rows plus the 1-row border-bottom) but genuinely scrollable
when there are more todos than that — and auto-scrolls to keep whichever
task is 'in_progress' visible whenever the list changes. Collapses to
nothing when there are no todos. (Subagent progress lives in StatusBar's
second line, not here.)"""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from pcli.session.models import TodoItem
from pcli.tui.widgets.status_bar import truncate

_STATUS_ICONS = {"pending": "○", "in_progress": "▶", "completed": "✓"}
_STATUS_STYLES = {"pending": "", "in_progress": "bold", "completed": "dim"}


class StatusPane(VerticalScroll):
    todos: reactive[list[TodoItem]] = reactive(list)

    def on_mount(self) -> None:
        self._sync_visibility()

    async def watch_todos(self, todos: list[TodoItem]) -> None:
        await self.remove_children()
        in_progress_widget: Static | None = None
        for todo in todos:
            widget = Static(
                Text(
                    f"{_STATUS_ICONS.get(todo.status, '?')} {truncate(todo.content, 76)}",
                    style=_STATUS_STYLES.get(todo.status, ""),
                ),
                classes="status-pane-todo",
            )
            # Static.render() returns an internal RichVisual wrapper, not the
            # raw Text — stash the source item directly for testability
            # rather than fighting that (same workaround MessageView uses).
            widget.todo = todo
            await self.mount(widget)
            if todo.status == "in_progress":
                in_progress_widget = widget

        self._sync_visibility()
        # Deferred to after layout has settled, same pattern Textual's own
        # Collapsible uses for its post-toggle auto-scroll.
        if in_progress_widget is not None:
            self.call_after_refresh(in_progress_widget.scroll_visible, animate=False)
        else:
            self.call_after_refresh(self.scroll_home, animate=False)

    def _sync_visibility(self) -> None:
        self.styles.display = "block" if self.todos else "none"
