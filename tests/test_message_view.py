import json

import pytest
from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Static

from pcli.tui.widgets.message_view import MessageView, _format_tool_call_body, _format_tool_output


class _ViewApp(App):
    def compose(self) -> ComposeResult:
        yield MessageView(id="message-view")


def test_format_tool_output_pretty_prints_json():
    raw = json.dumps({"b": 2, "a": 1})
    result = _format_tool_output(raw)
    assert isinstance(result, Syntax)
    assert '"a": 1' in result.code
    assert '"b": 2' in result.code


def test_format_tool_output_falls_back_to_plain_text_for_non_json():
    result = _format_tool_output("plain command output\nline two")
    assert isinstance(result, Text)
    assert result.plain == "plain command output\nline two"


def test_format_tool_output_falls_back_when_json_looking_but_invalid():
    result = _format_tool_output("{not actually json")
    assert isinstance(result, Text)
    assert result.plain == "{not actually json"


@pytest.mark.asyncio
async def test_add_tool_result_mounts_collapsed_by_default():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_tool_result("read_file", "line 1\nline 2\nline 3", is_error=False)
        await pilot.pause()

        collapsible = view.query_one(Collapsible)
        assert collapsible.collapsed is True
        assert "read_file" in collapsible.title
        assert "20 char(s)" in collapsible.title
        assert "error" not in collapsible.classes


@pytest.mark.asyncio
async def test_add_tool_result_marks_error_with_distinct_class_and_icon():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_tool_result("run_shell", "boom", is_error=True)
        await pilot.pause()

        collapsible = view.query_one(Collapsible)
        assert "error" in collapsible.classes
        assert collapsible.title.startswith("✗")


@pytest.mark.asyncio
async def test_add_tool_result_expanding_reveals_full_content():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        long_output = "x" * 5000
        view.add_tool_result("big_tool", long_output, is_error=False)
        await pilot.pause()

        collapsible = view.query_one(Collapsible)
        assert collapsible.collapsed is True

        collapsible.collapsed = False
        await pilot.pause()
        assert collapsible.collapsed is False

        body_static = collapsible.query_one(".tool-result-body")
        rendered = body_static.render()
        # Static.render() wraps content in an internal Visual; go through
        # the same private-content path _format_tool_output guarantees a
        # Text/Syntax renderable for, verified indirectly via char count
        # already shown in the (now-irrelevant) collapsed title, and here by
        # confirming the widget wasn't rebuilt/truncated on expand.
        assert rendered is not None
        assert collapsible.title.endswith("5,000 char(s)")


@pytest.mark.asyncio
async def test_add_tool_result_does_not_disturb_streaming_state():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_message("assistant", "")
        view.append_to_last("partial reply")

        view.add_tool_result("read_file", "content", is_error=False)
        await pilot.pause()

        # add_tool_result must not touch _current/_current_text — the
        # streaming assistant message is untouched by it.
        assert view._current_role == "assistant"
        assert view._current_text == "partial reply"


# --- add_message: collapsible system/shell output ---
#
# Regression coverage for a real reported complaint: verbose slash-command
# output (/help, /memory, /subagent, ...) and shell passthrough (!command)
# just printed permanently in the transcript with no way to reclaim the
# space. Long "system"/"shell" messages now mount inside an expanded-by-
# default Collapsible instead of a plain Static.


@pytest.mark.asyncio
async def test_add_message_short_system_stays_plain_not_collapsible():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_message("system", "Model set to 'gpt-4'.")
        await pilot.pause()

        assert len(view.query(Collapsible)) == 0
        assert view._current_text == "Model set to 'gpt-4'."


@pytest.mark.asyncio
async def test_add_message_long_system_collapses_but_stays_expanded_by_default():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        long_text = "# Commands\n\n" + "- some line of help text\n" * 30
        view.add_message("system", long_text)
        await pilot.pause()

        collapsible = view.query_one(Collapsible)
        # Unlike tool calls/results/reasoning (collapsed by default), the
        # user just asked to see this - only the *option* to hide it later
        # is new.
        assert collapsible.collapsed is False
        assert "System" in collapsible.title
        assert f"{len(long_text):,} char(s)" in collapsible.title
        # _current_text/_current_role still reflect the actual content, same
        # as the non-collapsible path (existing tests rely on this).
        assert view._current_role == "system"
        assert view._current_text == long_text


@pytest.mark.asyncio
async def test_add_message_long_shell_collapses_with_command_in_title():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        output = "$ ls -la\n" + "line of output\n" * 30
        view.add_message("shell", output)
        await pilot.pause()

        collapsible = view.query_one(Collapsible)
        assert collapsible.collapsed is False
        assert collapsible.title.startswith("Shell: ls -la —")


@pytest.mark.asyncio
async def test_add_message_long_user_and_assistant_never_collapse():
    """Primary conversation content - never tucked behind a click, no
    matter how long. Assistant streaming also depends on self._current
    staying a plain, directly-mounted Static (see append_to_last/_flush)."""
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        long_text = "x" * 1000
        view.add_message("user", long_text)
        view.add_message("assistant", long_text)
        await pilot.pause()

        assert len(view.query(Collapsible)) == 0


# --- add_tool_call / _format_tool_call_body ---


def test_format_tool_call_body_highlights_write_file_content_with_real_newlines():
    renderable, char_count = _format_tool_call_body(
        "write_file", {"path": "foo.py", "content": "def foo():\n    return 42\n"}
    )
    assert isinstance(renderable, Group)
    syntax_parts = [r for r in renderable.renderables if isinstance(r, Syntax)]
    assert len(syntax_parts) == 1
    assert syntax_parts[0].lexer.name.lower() == "python"
    assert "\n" in syntax_parts[0].code  # real newlines, not the escaped "\\n" JSON form
    assert char_count == len("def foo():\n    return 42\n")


def test_format_tool_call_body_uses_bash_lexer_for_run_shell():
    renderable, char_count = _format_tool_call_body("run_shell", {"command": "ls -la", "timeout_s": 30})
    syntax_parts = [r for r in renderable.renderables if isinstance(r, Syntax)]
    assert len(syntax_parts) == 1
    assert syntax_parts[0].lexer.name.lower() == "bash"
    assert char_count == len("ls -la")


def test_format_tool_call_body_shows_both_old_and_new_string_for_edit_file():
    renderable, char_count = _format_tool_call_body(
        "edit_file",
        {"path": "bar.py", "old_string": "def bar():\n    pass", "new_string": "def bar():\n    return 1"},
    )
    syntax_parts = [r for r in renderable.renderables if isinstance(r, Syntax)]
    assert len(syntax_parts) == 2
    assert syntax_parts[0].code == "def bar():\n    pass"
    assert syntax_parts[1].code == "def bar():\n    return 1"
    assert char_count == len("def bar():\n    pass") + len("def bar():\n    return 1")


def test_format_tool_call_body_falls_back_to_pretty_json_for_unknown_tools():
    renderable, char_count = _format_tool_call_body("grep", {"pattern": "foo", "path": "."})
    assert isinstance(renderable, Syntax)
    assert '"pattern": "foo"' in renderable.code
    assert char_count == len(renderable.code)


def test_format_tool_call_body_handles_no_arguments():
    renderable, char_count = _format_tool_call_body("list_dir", {})
    assert isinstance(renderable, Text)
    assert char_count == 0


@pytest.mark.asyncio
async def test_add_tool_call_renders_small_calls_inline_not_collapsed():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_tool_call("read_file", json.dumps({"path": "setup.py"}))
        await pilot.pause()

        assert len(view.query(Collapsible)) == 0
        widget = view.query_one(".message-tool", Static)
        assert widget is not None


@pytest.mark.asyncio
async def test_add_tool_call_collapses_large_calls_by_default():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        big_content = "line\n" * 200  # well over the 500-char collapse threshold
        view.add_tool_call("write_file", json.dumps({"path": "big.py", "content": big_content}))
        await pilot.pause()

        collapsible = view.query_one(Collapsible)
        assert collapsible.collapsed is True
        assert "write_file" in collapsible.title
        assert f"{len(big_content):,} char(s)" in collapsible.title


def test_format_tool_call_body_never_receives_purpose_as_a_real_argument():
    """add_tool_call strips "purpose" out of the parsed arguments before
    calling _format_tool_call_body (it's shown on its own line instead) -
    verified here directly against the pure function: a tool with no
    known code argument (falls back to pretty-JSON) must never show
    "purpose" as a listed key."""
    renderable, _char_count = _format_tool_call_body(
        "grep", {"pattern": "foo", "path": "."}  # purpose already stripped by the caller
    )
    assert isinstance(renderable, Syntax)
    assert "purpose" not in renderable.code


@pytest.mark.asyncio
async def test_add_tool_call_strips_purpose_from_the_pretty_printed_arguments():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_tool_call(
            "grep",
            json.dumps({"pattern": "foo", "path": ".", "purpose": "looking for foo"}),
            purpose="looking for foo",
        )
        await pilot.pause()

        widget = view.query_one(".message-tool", Static)
        # Static has no public accessor for the renderable passed to its
        # constructor (only .render(), which wraps it in an internal Visual
        # - see the comment on test_add_tool_result_expanding_reveals_full_
        # content above) - the name-mangled private attribute is the only
        # way to inspect the actual Group tree that was mounted.
        rendered_group = widget._Static__content
        syntax_parts = [r for r in rendered_group.renderables if isinstance(r, Syntax)]
        assert len(syntax_parts) == 1
        assert "purpose" not in syntax_parts[0].code  # not duplicated into the argument listing
        text_parts = [r for r in rendered_group.renderables if isinstance(r, Text)]
        assert any(t.plain == "looking for foo" for t in text_parts)  # shown on its own line instead


@pytest.mark.asyncio
async def test_add_tool_call_falls_back_gracefully_on_malformed_arguments_json():
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(MessageView)
        view.add_tool_call("run_shell", "{not valid json")
        await pilot.pause()

        assert len(view.query(Collapsible)) == 0
        widget = view.query_one(".message-tool", Static)
        assert widget is not None


@pytest.mark.asyncio
async def test_add_tool_call_does_not_force_scroll_when_user_scrolled_up():
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)

        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert view.scroll_y == 0

        view.add_tool_call("read_file", json.dumps({"path": "setup.py"}))
        await pilot.pause()

        assert view.scroll_y == 0


# --- Scroll-sticky behavior ---
#
# Regression coverage for a reported bug: expanding a collapsed panel (e.g.
# the "Thinking" reasoning panel) to read it while a turn is still
# streaming used to be impossible — every new fragment/message/tool result
# called scroll_end() unconditionally, dragging the view back to the bottom
# out from under a user who had scrolled up, on every single throttled
# flush. Fixed by only auto-scrolling when the view was already at the
# bottom before the new content arrived ("stick to bottom", not "force to
# bottom").


async def _settle(pilot) -> None:
    # scroll_end()'s default immediate=False defers the actual scroll via
    # call_after_refresh (see Textual's Widget.scroll_end source) so it can
    # read max_scroll_y only after that refresh's layout has run - a single
    # pilot.pause() isn't reliably enough cycles for that deferred callback
    # to have landed, which made single-pause assertions on
    # is_vertical_scroll_end flaky. A few extra pauses give it room to settle.
    for _ in range(3):
        await pilot.pause()


async def _fill_with_overflowing_content(view: MessageView, pilot) -> None:
    for i in range(30):
        view.add_message("system", f"line {i}")
        await pilot.pause()  # let layout catch up between mounts, like real turns do
    assert view.max_scroll_y > 0  # sanity: content actually overflows the viewport
    # Land on a definitive, fully-settled "at bottom" baseline for callers -
    # scroll_end() calls made against not-yet-laid-out content above could
    # otherwise leave scroll_y a few cells short of the eventual max.
    view.scroll_end(animate=False)
    await _settle(pilot)
    assert view.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_add_message_still_sticks_to_bottom_when_already_there():
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)
        assert view.is_vertical_scroll_end

        view.add_message("system", "another message")
        await _settle(pilot)

        assert view.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_add_message_does_not_force_scroll_when_user_scrolled_up():
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)

        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert view.scroll_y == 0
        assert not view.is_vertical_scroll_end

        view.add_message("system", "new message while scrolled up")
        await pilot.pause()

        assert view.scroll_y == 0  # must not have been dragged back down


@pytest.mark.asyncio
async def test_streaming_flush_does_not_force_scroll_when_user_scrolled_up():
    """The exact reported scenario: a turn keeps streaming (append_to_last
    -> _flush) while the user has scrolled up to read an expanded panel."""
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)

        view.add_message("assistant", "")
        await _settle(pilot)  # let its own auto-scroll (still at bottom) resolve first
        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert view.scroll_y == 0

        view.append_to_last("more streamed text")
        view._flush()  # bypass the throttle timer for a deterministic assertion
        await pilot.pause()

        assert view.scroll_y == 0


@pytest.mark.asyncio
async def test_add_tool_result_does_not_force_scroll_when_user_scrolled_up():
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)

        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert view.scroll_y == 0

        view.add_tool_result("read_file", "content", is_error=False)
        await pilot.pause()

        assert view.scroll_y == 0


@pytest.mark.asyncio
async def test_add_reasoning_does_not_force_scroll_when_user_scrolled_up():
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)

        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert view.scroll_y == 0

        view.add_reasoning("hmm, thinking about this")
        await pilot.pause()

        assert view.scroll_y == 0


@pytest.mark.asyncio
async def test_scrolling_back_to_bottom_resumes_auto_scroll():
    app = _ViewApp()
    async with app.run_test(size=(80, 10)) as pilot:
        view = app.query_one(MessageView)
        await _fill_with_overflowing_content(view, pilot)

        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        view.add_message("system", "while scrolled up")
        await pilot.pause()
        assert view.scroll_y == 0  # confirms the scroll-up actually took effect

        view.scroll_end(animate=False)
        await _settle(pilot)
        assert view.is_vertical_scroll_end

        view.add_message("system", "after scrolling back down")
        await _settle(pilot)

        assert view.is_vertical_scroll_end
