import pytest
from textual import events
from textual.app import App, ComposeResult

from pcli.tui.widgets.paste_input import PasteInput


class _InputApp(App):
    def compose(self) -> ComposeResult:
        yield PasteInput(id="input-box")


class _ExpandingInputApp(App):
    def compose(self) -> ComposeResult:
        yield PasteInput(id="input-box", expand_full_paste=True)


@pytest.mark.asyncio
async def test_shift_insert_is_bound_to_paste_from_os_clipboard():
    app = _InputApp()
    async with app.run_test():
        field = app.query_one(PasteInput)
        keys = list(field._bindings.key_to_bindings.keys())
        assert "shift+insert" in keys
        # ctrl+v (and every other Input binding) is still present — the
        # subclass's BINDINGS merge with the parent's, not replace them.
        assert "ctrl+v" in keys
        assert "left" in keys


@pytest.mark.asyncio
async def test_paste_from_os_clipboard_inserts_only_first_line(monkeypatch):
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "first line\nsecond line")
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "first line"


@pytest.mark.asyncio
async def test_paste_from_os_clipboard_inserts_at_cursor(monkeypatch):
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()
        field.value = "hello world"
        field.cursor_position = 5  # right after "hello"

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: " there")
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "hello there world"


@pytest.mark.asyncio
async def test_paste_from_os_clipboard_replaces_selection(monkeypatch):
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()
        field.value = "hello world"
        field.selection = (0, 5)  # select "hello"

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "goodbye")
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "goodbye world"


@pytest.mark.asyncio
async def test_paste_from_os_clipboard_handles_empty_clipboard(monkeypatch):
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()
        field.value = "unchanged"

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "")
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "unchanged"


@pytest.mark.asyncio
async def test_paste_from_os_clipboard_notifies_on_read_failure(monkeypatch):
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()
        field.value = "unchanged"

        import pcli.tui.widgets.paste_input as module

        def _raise():
            raise module.pyperclip.PyperclipException("no clipboard mechanism found")

        monkeypatch.setattr(module.pyperclip, "paste", _raise)
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "unchanged"  # must not crash or corrupt the field


@pytest.mark.asyncio
async def test_expand_full_paste_shows_placeholder_for_multiline_clipboard(monkeypatch):
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        clipboard_text = "line one\nline two\nline three"
        monkeypatch.setattr(module.pyperclip, "paste", lambda: clipboard_text)
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "[Pasted 3 lines]"


@pytest.mark.asyncio
async def test_expand_full_paste_single_line_inserts_directly_no_placeholder(monkeypatch):
    """A single-line clipboard has nothing to elide, so it behaves exactly
    like the non-expanding mode — no placeholder needed."""
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "just one line")
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "just one line"


@pytest.mark.asyncio
async def test_consume_pending_paste_expands_placeholder_to_full_text(monkeypatch):
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        clipboard_text = "line one\nline two\nline three"
        monkeypatch.setattr(module.pyperclip, "paste", lambda: clipboard_text)
        field.action_paste_from_os_clipboard()
        await pilot.pause()
        assert field.value == "[Pasted 3 lines]"

        expanded = field.consume_pending_paste(field.value)
        assert expanded == clipboard_text


@pytest.mark.asyncio
async def test_consume_pending_paste_expands_placeholder_amid_other_text(monkeypatch):
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "a\nb")
        field.value = "explain this: "
        field.cursor_position = len(field.value)
        field.action_paste_from_os_clipboard()
        field.insert_text_at_cursor(" please")
        await pilot.pause()

        assert field.value == "explain this: [Pasted 2 lines] please"
        expanded = field.consume_pending_paste(field.value)
        assert expanded == "explain this: a\nb please"


def test_consume_pending_paste_leaves_unrelated_text_untouched():
    field = PasteInput(expand_full_paste=True)
    assert field.consume_pending_paste("plain typed text") == "plain typed text"


@pytest.mark.asyncio
async def test_consume_pending_paste_is_noop_if_placeholder_was_edited_away(monkeypatch):
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "a\nb")
        field.action_paste_from_os_clipboard()
        await pilot.pause()
        assert field.value == "[Pasted 2 lines]"

        # User deletes the placeholder and types something else instead.
        field.value = "never mind"
        expanded = field.consume_pending_paste(field.value)
        assert expanded == "never mind"


@pytest.mark.asyncio
async def test_consume_pending_paste_clears_state_so_it_cannot_be_reused(monkeypatch):
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "a\nb")
        field.action_paste_from_os_clipboard()
        await pilot.pause()
        marker = field.value

        first = field.consume_pending_paste(marker)
        assert first == "a\nb"
        # Same literal marker text submitted again later must not re-expand
        # to stale data — the pending state was consumed once already.
        second = field.consume_pending_paste(marker)
        assert second == marker


@pytest.mark.asyncio
async def test_non_expanding_input_never_produces_a_placeholder(monkeypatch):
    """The sessions-import path field (expand_full_paste=False, the
    default) must keep the original first-line-only behavior untouched."""
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        import pcli.tui.widgets.paste_input as module

        monkeypatch.setattr(module.pyperclip, "paste", lambda: "line one\nline two")
        field.action_paste_from_os_clipboard()
        await pilot.pause()

        assert field.value == "line one"
        assert field.consume_pending_paste(field.value) == field.value


# --- Terminal-intercepted paste (events.Paste / _on_paste) ---
#
# This is the path that actually fires in most real terminals: they
# intercept Shift+Insert (and middle-click, Ctrl+Shift+V, ...) themselves
# and deliver the clipboard via the already-enabled bracketed-paste ANSI
# channel, which Textual turns into an events.Paste message — never a
# literal "shift+insert" keypress, so action_paste_from_os_clipboard above
# never fires in that case. Dispatched here via post_message, the same way
# Textual's own driver would deliver it, not by calling a private method
# directly.


@pytest.mark.asyncio
async def test_terminal_paste_event_inserts_only_first_line_when_not_expanding():
    app = _InputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        field.post_message(events.Paste(text="first line\nsecond line"))
        await pilot.pause()

        assert field.value == "first line"


@pytest.mark.asyncio
async def test_terminal_paste_event_shows_placeholder_when_expanding():
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        field.post_message(events.Paste(text="line one\nline two\nline three"))
        await pilot.pause()

        assert field.value == "[Pasted 3 lines]"


@pytest.mark.asyncio
async def test_terminal_paste_event_single_line_inserts_directly_when_expanding():
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        field.post_message(events.Paste(text="just one line"))
        await pilot.pause()

        assert field.value == "just one line"


@pytest.mark.asyncio
async def test_terminal_paste_event_placeholder_expands_at_submit():
    app = _ExpandingInputApp()
    async with app.run_test() as pilot:
        field = app.query_one(PasteInput)
        field.focus()

        clipboard_text = "a\nb\nc"
        field.post_message(events.Paste(text=clipboard_text))
        await pilot.pause()
        assert field.value == "[Pasted 3 lines]"

        expanded = field.consume_pending_paste(field.value)
        assert expanded == clipboard_text
