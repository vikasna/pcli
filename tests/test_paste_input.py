import pytest
from textual.app import App, ComposeResult

from pcli.tui.widgets.paste_input import PasteInput


class _InputApp(App):
    def compose(self) -> ComposeResult:
        yield PasteInput(id="input-box")


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
