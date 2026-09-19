import pytest
from textual.app import App
from textual.widgets import Button

from pcli.tui.screens.confirm_modal import ConfirmModal


class _HostApp(App):
    pass


@pytest.mark.asyncio
async def test_clicking_confirm_dismisses_with_true():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = ConfirmModal("Are you sure?")
        dismissed_with: list[bool] = []
        modal.dismiss = dismissed_with.append

        app.push_screen(modal)
        await pilot.pause()

        modal.on_button_pressed(Button.Pressed(modal.query_one("#confirm-yes", Button)))

        assert dismissed_with == [True]


@pytest.mark.asyncio
async def test_clicking_cancel_dismisses_with_false():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = ConfirmModal("Are you sure?")
        dismissed_with: list[bool] = []
        modal.dismiss = dismissed_with.append

        app.push_screen(modal)
        await pilot.pause()

        modal.on_button_pressed(Button.Pressed(modal.query_one("#confirm-no", Button)))

        assert dismissed_with == [False]


@pytest.mark.asyncio
async def test_custom_button_labels():
    app = _HostApp()
    async with app.run_test() as pilot:
        modal = ConfirmModal("Proceed?", confirm_label="Yes, continue", cancel_label="No, stop")
        app.push_screen(modal)
        await pilot.pause()

        assert str(modal.query_one("#confirm-yes", Button).label) == "Yes, continue"
        assert str(modal.query_one("#confirm-no", Button).label) == "No, stop"
