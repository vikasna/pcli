"""Coverage for ChatInput: the TextArea-based main chat input — submit vs.
newline key handling, typing-only auto-grow, paste behavior identical to
PasteInput's placeholder mechanism (verified explicitly not to grow the
box), and shell-style Up/Down history recall."""

import pytest
from textual import events
from textual.app import App, ComposeResult

from pcli.tui.widgets.chat_input import ChatInput

_BASE_HEIGHT = 3  # 1 content row + 2 border rows


class _ChatInputApp(App):
    def compose(self) -> ComposeResult:
        yield ChatInput(id="chat-input")


class _SubmitCapturingApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[ChatInput.Submitted] = []

    def compose(self) -> ComposeResult:
        yield ChatInput(id="chat-input")

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        self.submitted.append(event)


@pytest.mark.asyncio
async def test_enter_submits_without_inserting_a_newline():
    app = _SubmitCapturingApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()

        assert len(app.submitted) == 1
        assert app.submitted[0].value == "hi"
        assert ci.text == "hi"  # unchanged - Enter must not insert "\n"


@pytest.mark.asyncio
async def test_ctrl_j_inserts_a_real_newline_and_grows_the_box():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        await pilot.press("h", "i")
        assert int(ci.styles.height.value) == _BASE_HEIGHT

        await pilot.press("ctrl+j")
        await pilot.press("t", "h", "e", "r", "e")
        await pilot.pause()

        assert ci.text == "hi\nthere"
        assert int(ci.styles.height.value) == _BASE_HEIGHT + 1


@pytest.mark.asyncio
async def test_alt_enter_also_inserts_a_newline():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        await pilot.press("a")
        await pilot.press("alt+enter")
        await pilot.press("b")
        await pilot.pause()

        assert ci.text == "a\nb"


@pytest.mark.asyncio
async def test_box_grows_up_to_max_and_no_further():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        for _ in range(15):
            await pilot.press("x")
            await pilot.press("ctrl+j")
        await pilot.pause()

        assert ci.document.line_count == 16
        assert int(ci.styles.height.value) == 10 + 2  # clamped at _MAX_VISIBLE_LINES


@pytest.mark.asyncio
async def test_multiline_paste_does_not_grow_the_box():
    """The specific behavior corrected in review: a large paste must stay
    collapsed behind the [Pasted N lines] placeholder, never expand the
    box the way real typed newlines do."""
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        assert int(ci.styles.height.value) == _BASE_HEIGHT

        ci.post_message(events.Paste(text="line one\nline two\nline three\nline four"))
        await pilot.pause()

        assert ci.text == "[Pasted 4 lines]"
        assert int(ci.styles.height.value) == _BASE_HEIGHT  # unchanged


@pytest.mark.asyncio
async def test_multiline_paste_placeholder_expands_at_submit():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        clipboard_text = "def add(a, b):\n    return a + b"
        ci.post_message(events.Paste(text=clipboard_text))
        await pilot.pause()
        assert ci.text == "[Pasted 2 lines]"

        expanded = ci.consume_pending_paste(ci.text)
        assert expanded == clipboard_text


@pytest.mark.asyncio
async def test_single_line_paste_inserts_directly_and_does_not_grow_box():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        ci.post_message(events.Paste(text="just one line"))
        await pilot.pause()

        assert ci.text == "just one line"
        assert int(ci.styles.height.value) == _BASE_HEIGHT


@pytest.mark.asyncio
async def test_shift_insert_paste_uses_the_same_placeholder_mechanism(monkeypatch):
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()

        import pcli.tui.widgets.chat_input as module

        clipboard_text = "line one\nline two\nline three"
        monkeypatch.setattr(module.pyperclip, "paste", lambda: clipboard_text)
        ci.action_paste_from_os_clipboard()
        await pilot.pause()

        assert ci.text == "[Pasted 3 lines]"
        assert int(ci.styles.height.value) == _BASE_HEIGHT


@pytest.mark.asyncio
async def test_escape_is_not_consumed_by_chat_input():
    """TextArea only eats escape when tab_behavior='indent' (code_editor()
    mode) - ChatInput uses the plain constructor default ('focus'), so
    escape must bubble to the Screen's own binding unobstructed."""
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        await pilot.press("escape")
        await pilot.pause()
        # No crash, no focus change forced by TextArea itself.
        assert app.focused is ci


# --- History ---


@pytest.mark.asyncio
async def test_history_recall_walks_oldest_to_newest_and_back_to_draft():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        ci.add_to_history("first")
        ci.add_to_history("second")
        ci.text = "unsent draft"
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()
        assert ci.text == "second"

        await pilot.press("up")
        await pilot.pause()
        assert ci.text == "first"

        await pilot.press("up")  # already at oldest - stays put
        await pilot.pause()
        assert ci.text == "first"

        await pilot.press("down")
        await pilot.pause()
        assert ci.text == "second"

        await pilot.press("down")  # past the newest - restores the draft
        await pilot.pause()
        assert ci.text == "unsent draft"


@pytest.mark.asyncio
async def test_history_navigation_disabled_while_editing_multiline_draft():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        ci.add_to_history("old message")
        ci.text = "line one\nline two"
        ci.move_cursor(ci.document.end)
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        # Up moved the cursor within the text, not into history.
        assert ci.text == "line one\nline two"


@pytest.mark.asyncio
async def test_history_is_empty_up_down_are_harmless():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        await pilot.press("up")
        await pilot.press("down")
        await pilot.pause()
        assert ci.text == ""


@pytest.mark.asyncio
async def test_add_to_history_resets_browsing_state():
    app = _ChatInputApp()
    async with app.run_test() as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        ci.add_to_history("first")
        ci.text = "draft"
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert ci.text == "first"

        ci.add_to_history("second")  # a real submission happened
        assert ci._history_index is None
        assert ci._draft_before_history == ""
