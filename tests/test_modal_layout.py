"""Regression coverage for the "questions/permissions dialogs pinned to top,
with slim buttons" layout change: the shared modal CSS in pcli.tcss used to
center these dialogs vertically (align: center middle), hiding roughly the
middle of the chat transcript behind an opaque box, with each button taking
3 rows (Textual's default bordered button height). Both changed: alignment
moved to the top of the screen, and buttons dropped their border for a
single-line height, so a lot more of the transcript stays visible below the
dialog."""

import pytest
from textual.app import App
from textual.widgets import Button

from pcli.tui.app import _STYLES_PATH
from pcli.tui.screens.ask_question_modal import AskQuestionModal
from pcli.tui.screens.permission_modal import PermissionPromptModal

_SCREEN_HEIGHT = 40


class _StyledHostApp(App):
    CSS_PATH = str(_STYLES_PATH)


@pytest.mark.asyncio
async def test_permission_modal_is_pinned_to_the_top_not_centered():
    app = _StyledHostApp()
    async with app.run_test(size=(100, _SCREEN_HEIGHT)) as pilot:
        await app.push_screen(PermissionPromptModal("run_shell", {"command": "echo hi"}, "risk"))
        await pilot.pause()

        box = app.screen.query_one("#permission-modal")
        # Pinned to the top means y == margin-top (1); centered (the old
        # behavior) would put this near the middle of a 40-row screen.
        assert box.region.y == 1


@pytest.mark.asyncio
async def test_permission_modal_buttons_are_single_line():
    app = _StyledHostApp()
    async with app.run_test(size=(100, _SCREEN_HEIGHT)) as pilot:
        await app.push_screen(PermissionPromptModal("run_shell", {"command": "echo hi"}, "risk"))
        await pilot.pause()

        for button in app.screen.query(Button):
            assert button.region.height == 1


@pytest.mark.asyncio
async def test_ask_question_modal_is_pinned_to_the_top_not_centered():
    app = _StyledHostApp()
    async with app.run_test(size=(100, _SCREEN_HEIGHT)) as pilot:
        await app.push_screen(AskQuestionModal("Pick one", ["A", "B", "C"]))
        await pilot.pause()

        box = app.screen.query_one("#ask-question-modal")
        # Pinned to the top means y == margin-top (1); centered (the old
        # behavior) would put this near the middle of a 40-row screen.
        assert box.region.y == 1


@pytest.mark.asyncio
async def test_ask_question_modal_option_buttons_are_single_line():
    app = _StyledHostApp()
    async with app.run_test(size=(100, _SCREEN_HEIGHT)) as pilot:
        await app.push_screen(AskQuestionModal("Pick one", ["A", "B", "C"]))
        await pilot.pause()

        for button in app.screen.query("#ask-question-options Button"):
            assert button.region.height == 1
