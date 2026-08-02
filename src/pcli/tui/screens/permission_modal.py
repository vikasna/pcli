"""Modal that implements PermissionManager's `ask` callback via Textual."""

from __future__ import annotations

from typing import Any, Literal

from textual.app import App, ComposeResult
from textual.containers import Grid, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

PermissionModalResult = tuple[Literal["allow", "deny"], Literal["once", "session", "always"] | None]

_BUTTONS: list[tuple[str, str, PermissionModalResult, str]] = [
    ("allow-once", "Allow Once", ("allow", "once"), "success"),
    ("allow-session", "Allow for Session", ("allow", "session"), "success"),
    ("allow-always", "Allow Always", ("allow", "always"), "warning"),
    ("deny", "Deny", ("deny", None), "error"),
]

# Tool arguments can carry arbitrarily long content (e.g. write_file's full
# text) — shown uncapped, that could otherwise dominate the whole dialog.
# Truncating keeps the common case compact; the modal is scrollable as a
# safety net regardless of terminal size (see #permission-modal).
_MAX_ARG_PREVIEW_CHARS = 800


def _format_arguments(arguments: dict[str, Any]) -> str:
    text = str(arguments)
    if len(text) > _MAX_ARG_PREVIEW_CHARS:
        omitted = len(text) - _MAX_ARG_PREVIEW_CHARS
        text = f"{text[:_MAX_ARG_PREVIEW_CHARS]}\n... [{omitted} more chars truncated]"
    return text


class PermissionPromptModal(ModalScreen[PermissionModalResult]):
    def __init__(
        self, tool_name: str, arguments: dict[str, Any], risk_description: str = ""
    ) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._arguments = arguments
        self._risk_description = risk_description

    def compose(self) -> ComposeResult:
        # The whole dialog is one scrollable region (not just the info text)
        # so the Allow/Deny buttons are always reachable — via mouse wheel,
        # or Tab, which Textual auto-scrolls into view — no matter how small
        # the terminal window is, instead of silently overflowing past it.
        with VerticalScroll(id="permission-modal"):
            yield Static(f"Tool wants to run: {self._tool_name}", id="permission-title")
            yield Static(_format_arguments(self._arguments), id="permission-args")
            if self._risk_description:
                yield Static(self._risk_description, id="permission-risk")
            with Grid(id="permission-buttons"):
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
