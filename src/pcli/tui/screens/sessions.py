"""Lists stored sessions; Enter resumes, 'e' exports, 'i' imports."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, ListItem, ListView, Static

from pcli.config.paths import data_dir
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.models import SessionIndexEntry
from pcli.session.store import SessionStore
from pcli.tui.widgets.paste_input import PasteInput

_ID_COL_WIDTH = 4
_TITLE_COL_WIDTH = 28
_MODEL_COL_WIDTH = 20
_COST_COL_WIDTH = 10


def _truncate(text: str, width: int) -> str:
    # Plain ASCII "..." rather than a Unicode ellipsis - some Windows
    # terminal/locale combinations mangle non-ASCII characters here.
    return text if len(text) <= width else text[: width - 3] + "..."


def _sessions_header_row() -> str:
    return (
        f"{'ID':<{_ID_COL_WIDTH}}  {'TITLE':<{_TITLE_COL_WIDTH}}  "
        f"{'MODEL':<{_MODEL_COL_WIDTH}}  {'COST':>{_COST_COL_WIDTH}}  UPDATED"
    )


def _sessions_row(entry: SessionIndexEntry) -> str:
    # Last 4 chars of the id rather than the full id — enough to
    # disambiguate at a glance (matched against /export's own filename,
    # which uses the full id) without dominating the row width.
    return (
        f"{entry.id[-4:]:<{_ID_COL_WIDTH}}  "
        f"{_truncate(entry.title, _TITLE_COL_WIDTH):<{_TITLE_COL_WIDTH}}  "
        f"{_truncate(entry.model or 'unknown model', _MODEL_COL_WIDTH):<{_MODEL_COL_WIDTH}}  "
        f"{f'${entry.total_cost_usd:.4f}':>{_COST_COL_WIDTH}}  "
        f"{entry.updated_at:%Y-%m-%d %H:%M}"
    )


class ImportPathModal(ModalScreen[str | None]):
    def compose(self) -> ComposeResult:
        with Vertical(id="import-modal"):
            yield Static("Path to a .pcli-session.json[.gz] file to import:")
            yield PasteInput(
                placeholder="/path/to/session.pcli-session.json", id="import-path-input"
            )
            yield Button("Import", id="import-confirm", variant="success")
            yield Button("Cancel", id="import-cancel", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "import-confirm":
            self.dismiss(self.query_one(Input).value.strip() or None)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class SessionListScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("e", "export_selected", "Export"),
        ("i", "import_session", "Import"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, store: SessionStore | None = None, *, exclude_session_id: str | None = None) -> None:
        super().__init__()
        self._store = store or SessionStore()
        self._exclude_session_id = exclude_session_id
        self._on_resume: object = None

    def compose(self) -> ComposeResult:
        yield Static("Sessions  (enter: resume, e: export, i: import, esc: back)", id="sessions-title")
        yield Static(_sessions_header_row(), id="sessions-header")
        yield ListView(id="session-list")

    def on_mount(self) -> None:
        # exclude_session_id protects the session that opened this screen -
        # it may still legitimately have zero messages (e.g. /sessions was
        # the very first thing typed) and must not be pruned out from under
        # the still-active ChatScreen holding it in memory.
        exclude = [self._exclude_session_id] if self._exclude_session_id else []
        self._store.prune_empty_sessions(exclude_session_ids=exclude)
        self._refresh()

    def _refresh(self) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        for entry in self._store.list_index():
            item = ListItem(Static(_sessions_row(entry)))
            item.data = entry.id  # type: ignore[attr-defined]
            list_view.append(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        session_id = getattr(event.item, "data", None)
        if not session_id:
            return
        from pcli.tui.screens.chat import ChatScreen

        session = self._store.load(session_id)
        self.app.pop_screen()
        self.app.push_screen(ChatScreen(session=session, store=self._store))

    def action_export_selected(self) -> None:
        list_view = self.query_one(ListView)
        highlighted = list_view.highlighted_child
        session_id = getattr(highlighted, "data", None) if highlighted else None
        if not session_id:
            return
        self._export(session_id)

    @work(exclusive=True)
    async def _export(self, session_id: str) -> None:
        session = self._store.load(session_id)
        out_dir = data_dir() / "exports"
        out_path = out_dir / f"{session.id}.pcli-session.json"
        export_session(session, out_path, store=self._store)
        self.notify(f"Exported to {out_path}")

    def action_import_session(self) -> None:
        self._import()

    @work(exclusive=True)
    async def _import(self) -> None:
        path_str = await self.app.push_screen_wait(ImportPathModal())
        if not path_str:
            return
        path = Path(path_str).expanduser()
        if not path.exists():
            self.notify(f"No such file: {path}", severity="error")
            return
        try:
            session = import_session(path, store=self._store)
        except Exception as exc:  # noqa: BLE001 - surface any import failure to the user
            self.notify(f"Import failed: {exc}", severity="error")
            return
        self.notify(f"Imported session {session.id}")
        self._refresh()
