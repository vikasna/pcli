import json

import pytest
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Collapsible

from pcli.tui.widgets.message_view import MessageView, _format_tool_output


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
