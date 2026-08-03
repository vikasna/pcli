import pytest
from textual.app import App, ComposeResult

from pcli.agent.activity import ActivityTracker
from pcli.session.models import TodoItem
from pcli.tui.widgets.status_pane import StatusPane


class _PaneApp(App):
    def compose(self) -> ComposeResult:
        yield StatusPane(id="status-pane")


def test_activity_tracker_reports_start_progress_and_finish():
    tracker = ActivityTracker()
    events = []
    tracker.subscribe(lambda: events.append(tracker.subagent))

    tracker.start_subagent("investigate the bug")
    assert tracker.subagent is not None
    assert tracker.subagent.task == "investigate the bug"
    assert tracker.subagent.tool_calls == 0

    tracker.record_subagent_tool_call("grep")
    tracker.record_subagent_tool_call("read_file")
    assert tracker.subagent.tool_calls == 2
    assert tracker.subagent.last_tool == "read_file"

    tracker.finish_subagent()
    assert tracker.subagent is None
    assert len(events) == 4  # start, 2 tool calls, finish


@pytest.mark.asyncio
async def test_status_pane_hidden_when_nothing_to_show():
    app = _PaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(StatusPane)
        await pilot.pause()
        assert str(pane.styles.display) == "none"


@pytest.mark.asyncio
async def test_status_pane_shows_todos_and_hides_completed_when_over_capacity():
    app = _PaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(StatusPane)
        pane.todos = [
            TodoItem(content="done task", status="completed"),
            TodoItem(content="active task", status="in_progress"),
            TodoItem(content="next task", status="pending"),
            TodoItem(content="later task", status="pending"),
        ]
        await pilot.pause()

        assert str(pane.styles.display) == "block"
        rendered = pane.render()
        text = "\n".join(segment.plain for segment in rendered.renderables)
        # in_progress is prioritized to the top over pending/completed.
        assert "active task" in text
        assert "next task" in text
        # Only 3 lines total; the 4th-ranked item is summarized, not listed.
        assert "later task" not in text
        assert "more todo(s)" in text
        assert "1/4 done" in text


@pytest.mark.asyncio
async def test_status_pane_shows_subagent_progress_and_shrinks_todo_lines():
    app = _PaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(StatusPane)
        pane.todos = [
            TodoItem(content="task one", status="in_progress"),
            TodoItem(content="task two", status="pending"),
        ]
        pane.subagent_task = "research something"
        pane.subagent_tool_calls = 3
        pane.subagent_last_tool = "grep"
        await pilot.pause()

        assert str(pane.styles.display) == "block"
        rendered = pane.render()
        text = "\n".join(segment.plain for segment in rendered.renderables)
        assert "Subagent" in text
        assert "research something" in text
        assert "3 tool call(s)" in text
        assert "grep" in text
        # Only 2 lines left for todos once the subagent line is shown.
        assert "task one" in text
        assert "task two" in text


@pytest.mark.asyncio
async def test_status_pane_hides_again_once_cleared():
    app = _PaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(StatusPane)
        pane.todos = [TodoItem(content="task", status="pending")]
        await pilot.pause()
        assert str(pane.styles.display) == "block"

        pane.todos = []
        await pilot.pause()
        assert str(pane.styles.display) == "none"
