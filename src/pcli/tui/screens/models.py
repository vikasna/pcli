"""Modal for picking a model, fetched live from the gateway's /models endpoint."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static


class ModelListScreen(ModalScreen[str | None]):
    """Shows the models reported by the gateway; Enter picks one, Escape cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_none", "Cancel")]

    def __init__(self, models: list[str], current: str | None = None) -> None:
        super().__init__()
        self._models = models
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="model-list-modal"):
            yield Static("Select a model  (enter: choose, esc: cancel)", id="model-list-title")
            yield ListView(id="model-list")

    def on_mount(self) -> None:
        list_view = self.query_one(ListView)
        for model_id in self._models:
            marker = "* " if model_id == self._current else "  "
            item = ListItem(Static(f"{marker}{model_id}"))
            item.data = model_id  # type: ignore[attr-defined]
            list_view.append(item)
        if self._current in self._models:
            list_view.index = self._models.index(self._current)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "data", None))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
