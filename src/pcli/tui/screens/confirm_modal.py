"""Generic Yes/No confirmation modal - currently used for the
directory-mismatch warning when resuming a session from a different
directory than it was created in (see session/directory_check.py)."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    def __init__(
        self, message: str, *, confirm_label: str = "Continue", cancel_label: str = "Cancel"
    ) -> None:
        super().__init__()
        self._message = message
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-modal"):
            yield Static(self._message, id="confirm-message")
            yield Button(self._confirm_label, id="confirm-yes", variant="warning")
            yield Button(self._cancel_label, id="confirm-no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


async def confirm_via_modal(
    app: App[Any], message: str, *, confirm_label: str = "Continue", cancel_label: str = "Cancel"
) -> bool:
    return await app.push_screen_wait(
        ConfirmModal(message, confirm_label=confirm_label, cancel_label=cancel_label)
    )
