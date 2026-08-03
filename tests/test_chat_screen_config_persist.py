import tomllib
from pathlib import Path

import pytest
from textual.app import App

from pcli.config import settings as settings_module
from pcli.config.settings import Settings
from pcli.session.models import Session
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


@pytest.mark.asyncio
async def test_models_command_persists_selection_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: config_path)

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="old-model", gateway_base_url="")
    # Gateway left unconfigured on purpose: /models with an explicit name
    # doesn't need to reach the gateway, so this also proves it works even
    # when the app couldn't start the agent loop.
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/models new-model")
        await pilot.pause()

    assert settings.default_model == "new-model"
    assert session.model == "new-model"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_model"] == "new-model"
