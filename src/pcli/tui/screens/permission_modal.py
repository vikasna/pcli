"""Modal that implements PermissionManager's `ask` callback via Textual."""

from __future__ import annotations

from typing import Any, Literal

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

PermissionModalResult = tuple[Literal["allow", "deny"], Literal["once", "session", "always"] | None]

_BUTTONS: list[tuple[str, str, PermissionModalResult, str]] = [
    ("allow-once", "Allow Once", ("allow", "once"), "success"),
    ("allow-session", "Allow for Session", ("allow", "session"), "success"),
    ("allow-always", "Allow Always", ("allow", "always"), "warning"),
    ("deny", "Deny", ("deny", None), "error"),
]


class PermissionPromptModal(ModalScreen[PermissionModalResult]):
    def __init__(
        self, tool_name: str, arguments: dict[str, Any], risk_description: str = ""
    ) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._arguments = arguments
        self._risk_description = risk_description

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-modal"):
            yield Static(f"Tool wants to run: {self._tool_name}", id="permission-title")
            yield Static(str(self._arguments), id="permission-args")
            if self._risk_description:
                yield Static(self._risk_description, id="permission-risk")
            for button_id, label, _result, variant in _BUTTONS:
                yield Button(label, id=button_id, variant=variant)  # type: ignore[arg-type]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        for button_id, _label, result, _variant in _BUTTONS:
            if event.button.id == button_id:
                self.dismiss(result)
                return


async def ask_via_modal(
    app: App[Any], tool_name: str, arguments: dict[str, Any], risk_description: str
) -> PermissionModalResult:
    return await app.push_screen_wait(
        PermissionPromptModal(tool_name, arguments, risk_description)
    )
