from pcli.agent.activity import ActivityTracker


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
