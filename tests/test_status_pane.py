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
async def test_status_pane_shows_all_todos_as_individual_widgets_in_order():
    app = _PaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(StatusPane)
        pane.todos = [
            TodoItem(content="task one", status="completed"),
            TodoItem(content="task two", status="in_progress"),
            TodoItem(content="task three", status="pending"),
            TodoItem(content="task four", status="pending"),
        ]
        await pilot.pause()

        assert str(pane.styles.display) == "block"
        children = list(pane.query(".status-pane-todo"))
        # All 4 present (not truncated to 3 with a "+N more" summary) —
        # genuinely scrollable now instead.
        assert len(children) == 4
        # Original list order preserved (not reprioritized) — scrolling
        # handles bringing the active task into view instead of reordering.
        assert [w.todo.content for w in children] == [
            "task one",
            "task two",
            "task three",
            "task four",
        ]
        assert [w.todo.status for w in children] == [
            "completed",
            "in_progress",
            "pending",
            "pending",
        ]


@pytest.mark.asyncio
async def test_status_pane_auto_scrolls_to_in_progress_task():
    app = _PaneApp()
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(StatusPane)
        pane.todos = [
            *(TodoItem(content=f"done {i}", status="completed") for i in range(10)),
            TodoItem(content="the active one", status="in_progress"),
            TodoItem(content="upcoming", status="pending"),
        ]
        await pilot.pause()
        await pilot.pause()  # let the deferred call_after_refresh scroll run

        in_progress_widget = next(
            w for w in pane.query(".status-pane-todo") if w.todo.status == "in_progress"
        )
        pane_region = pane.region
        widget_region = in_progress_widget.region
        assert widget_region.y >= pane_region.y
        assert widget_region.y + widget_region.height <= pane_region.y + pane_region.height


@pytest.mark.asyncio
async def test_status_pane_scrolls_home_when_nothing_in_progress():
    app = _PaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(StatusPane)
        pane.todos = [TodoItem(content=f"task {i}", status="pending") for i in range(10)]
        await pilot.pause()
        await pilot.pause()

        assert pane.scroll_offset.y == 0


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
