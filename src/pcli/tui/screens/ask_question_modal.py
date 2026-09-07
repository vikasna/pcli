"""Modal that implements the ask_user_question tool's ask_question
callback via Textual — lets the model pause a turn to ask the user
something directly, optionally with a fixed set of suggested answers,
mirroring opencode's "ask" tool. A free-text field is always available
regardless of whether options are given, so a suggested answer never
forecloses a different one."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from pcli.tui.widgets.paste_input import PasteInput


class AskQuestionModal(ModalScreen[str]):
    def __init__(self, question: str, options: list[str] | None = None) -> None:
        super().__init__()
        self._question = question
        self._options = options or []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="ask-question-modal"):
            yield Static(self._question, id="ask-question-text")
            if self._options:
                with Vertical(id="ask-question-options"):
                    for index, option in enumerate(self._options):
                        yield Button(option, id=f"ask-question-option-{index}")
            yield PasteInput(
                placeholder="Or type your own answer and press Enter...",
                id="ask-question-input",
            )

    def on_mount(self) -> None:
        self.query_one(PasteInput).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        for index, option in enumerate(self._options):
            if event.button.id == f"ask-question-option-{index}":
                self.dismiss(option)
                return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        answer = event.value.strip()
        if answer:
            self.dismiss(answer)


async def ask_question_via_modal(app: App[Any], question: str, options: list[str] | None) -> str:
    return await app.push_screen_wait(AskQuestionModal(question, options))
