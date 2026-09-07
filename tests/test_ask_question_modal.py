"""Coverage for AskQuestionModal: the UI ask_user_question's ask_question
callback pushes, mirroring opencode's "ask" tool with an always-available
free-text field alongside any suggested options."""

import pytest
from textual.app import App
from textual.widgets import Button, Input

from pcli.tui.screens.ask_question_modal import AskQuestionModal


class _HostApp(App):
    pass


@pytest.mark.asyncio
async def test_clicking_an_option_button_dismisses_with_that_option():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = AskQuestionModal("Pick one", ["A", "B", "C"])
        dismissed_with: list[str] = []
        modal.dismiss = dismissed_with.append

        app.push_screen(modal)
        await pilot.pause()

        option_b = modal.query_one("#ask-question-option-1", Button)
        modal.on_button_pressed(Button.Pressed(option_b))

        assert dismissed_with == ["B"]


@pytest.mark.asyncio
async def test_submitting_free_text_dismisses_with_that_text():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = AskQuestionModal("What's your favorite color?")
        dismissed_with: list[str] = []
        modal.dismiss = dismissed_with.append

        app.push_screen(modal)
        await pilot.pause()

        text_input = modal.query_one(Input)
        modal.on_input_submitted(Input.Submitted(text_input, "blue"))

        assert dismissed_with == ["blue"]


@pytest.mark.asyncio
async def test_free_text_is_still_available_alongside_options():
    """The whole point of always showing the field: a suggested option
    never forecloses a different answer."""
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = AskQuestionModal("Pick a color", ["red", "blue"])
        dismissed_with: list[str] = []
        modal.dismiss = dismissed_with.append

        app.push_screen(modal)
        await pilot.pause()

        text_input = modal.query_one(Input)
        modal.on_input_submitted(Input.Submitted(text_input, "green"))

        assert dismissed_with == ["green"]


@pytest.mark.asyncio
async def test_submitting_empty_text_does_not_dismiss():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = AskQuestionModal("Anything to add?")
        dismissed_with: list[str] = []
        modal.dismiss = dismissed_with.append

        app.push_screen(modal)
        await pilot.pause()

        text_input = modal.query_one(Input)
        modal.on_input_submitted(Input.Submitted(text_input, "   "))

        assert dismissed_with == []


@pytest.mark.asyncio
async def test_with_no_options_only_the_text_field_is_shown():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = AskQuestionModal("Question with no options")
        app.push_screen(modal)
        await pilot.pause()

        assert len(modal.query(Button)) == 0
        assert len(modal.query(Input)) == 1
