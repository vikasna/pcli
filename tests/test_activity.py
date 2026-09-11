from pcli.agent.activity import (
    ActivityTracker,
    SubagentActivity,
    SubagentToolCall,
    format_subagent_activity,
)


def test_record_subagent_tool_call_updates_counter_and_call_log():
    activity = ActivityTracker()
    activity.start_subagent("do the thing")

    activity.record_subagent_tool_call("read_file", '{"path": "a.py"}')
    activity.record_subagent_tool_call("run_shell", '{"command": "ls"}')

    sub = activity.subagent
    assert sub is not None
    assert sub.tool_calls == 2
    assert sub.last_tool == "run_shell"
    assert [c.name for c in sub.call_log] == ["read_file", "run_shell"]
    assert sub.call_log[0].arguments == '{"path": "a.py"}'


def test_record_subagent_tool_call_before_start_is_a_noop():
    activity = ActivityTracker()
    activity.record_subagent_tool_call("read_file", "{}")  # no subagent running
    assert activity.subagent is None


def test_pending_question_set_and_cleared():
    activity = ActivityTracker()
    activity.start_subagent("do the thing")

    activity.set_subagent_pending_question("Which dataset?", ["A", "B"])
    assert activity.subagent.pending_question == ("Which dataset?", ["A", "B"])

    activity.clear_subagent_pending_question()
    assert activity.subagent.pending_question is None


def test_finish_subagent_clears_everything():
    activity = ActivityTracker()
    activity.start_subagent("do the thing")
    activity.record_subagent_tool_call("read_file", "{}")
    activity.set_subagent_pending_question("Q?", None)

    activity.finish_subagent()
    assert activity.subagent is None


def test_subscribers_are_notified_on_every_change():
    activity = ActivityTracker()
    calls = []
    activity.subscribe(lambda: calls.append(None))

    activity.start_subagent("t")
    activity.record_subagent_tool_call("x", "{}")
    activity.set_subagent_pending_question("q", None)
    activity.clear_subagent_pending_question()
    activity.finish_subagent()

    assert len(calls) == 5


def test_multiple_subscribers_all_get_notified():
    """Regression coverage for a real bug: subscribe() used to overwrite a
    single slot, so a second subscriber (the Ctrl+G live-activity modal)
    would silently replace the status bar's own callback instead of both
    being notified."""
    activity = ActivityTracker()
    first_calls = []
    second_calls = []
    activity.subscribe(lambda: first_calls.append(None))
    activity.subscribe(lambda: second_calls.append(None))

    activity.start_subagent("t")

    assert len(first_calls) == 1
    assert len(second_calls) == 1


def test_unsubscribe_stops_further_notifications_without_affecting_others():
    activity = ActivityTracker()
    first_calls = []
    second_calls = []

    def first():
        first_calls.append(None)

    def second():
        second_calls.append(None)

    activity.subscribe(first)
    activity.subscribe(second)
    activity.start_subagent("t")
    activity.unsubscribe(first)
    activity.record_subagent_tool_call("x", "{}")

    assert len(first_calls) == 1  # only got the start_subagent notification
    assert len(second_calls) == 2  # start_subagent + record_subagent_tool_call


def test_unsubscribe_unknown_callback_is_a_noop():
    activity = ActivityTracker()
    activity.unsubscribe(lambda: None)  # never subscribed - must not raise


def test_format_subagent_activity_includes_task_call_log_and_pending_question():
    sub = SubagentActivity(
        task="build the report",
        call_log=[
            SubagentToolCall(name="read_file", arguments='{"path": "data.csv"}'),
            SubagentToolCall(name="run_shell", arguments='{"command": "python train.py"}'),
        ],
        pending_question=("Overwrite report.html?", ["Yes", "No"]),
    )

    text = format_subagent_activity(sub)

    assert "build the report" in text
    assert "Tool calls so far: 2" in text
    assert "1. read_file(" in text
    assert "2. run_shell(" in text
    assert "Overwrite report.html?" in text
    assert "Yes" in text and "No" in text


def test_format_subagent_activity_omits_pending_question_section_when_none():
    sub = SubagentActivity(task="t", call_log=[])
    text = format_subagent_activity(sub)
    assert "waiting on your answer" not in text
