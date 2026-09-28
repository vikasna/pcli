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


@pytest.mark.asyncio
async def test_timeout_command_updates_settings_live_and_persists_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """GatewayClient reads request_timeout_s fresh per-request (no client
    rebuild needed), so /timeout only needs to update the shared Settings
    object in place - which it does, since ChatScreen and GatewayClient
    hold a reference to the very same Settings instance."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: config_path)

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="", request_timeout_s=120.0)

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/timeout 900")
        await pilot.pause()

    assert settings.request_timeout_s == 900.0
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["request_timeout_s"] == 900.0


@pytest.mark.asyncio
async def test_timeout_command_with_no_argument_reports_current_value(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="", request_timeout_s=120.0)

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        screen._handle_command("/timeout")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "120" in message_view._current_text
        assert settings.request_timeout_s == 120.0  # unchanged


@pytest.mark.asyncio
async def test_timeout_command_notes_the_local_api_floor_when_it_applies(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(
        gateway_base_url="http://localhost:1234/v1",
        gateway_api_key="",
        request_timeout_s=120.0,
        local_api_gateways=["http://localhost:1234/v1"],
    )

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        screen._handle_command("/timeout")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "120" in message_view._current_text
        assert "600" in message_view._current_text  # the floored effective value


@pytest.mark.asyncio
async def test_timeout_command_rejects_non_numeric_and_non_positive_values(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="", request_timeout_s=120.0)

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        message_view = screen.query_one(MessageView)

        screen._handle_command("/timeout not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text
        assert settings.request_timeout_s == 120.0

        screen._handle_command("/timeout -5")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text
        assert settings.request_timeout_s == 120.0


@pytest.mark.asyncio
async def test_temperature_command_updates_settings_live_and_persists_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: config_path)

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/temperature 0.5")
        await pilot.pause()

    assert settings.default_temperature == 0.5
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_temperature"] == 0.5


@pytest.mark.asyncio
async def test_temperature_off_clears_settings_and_removes_the_persisted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """update_config_file alone can't express this - it skips writing a
    None value rather than persisting the removal, so /temperature off
    goes through remove_config_keys instead (see its docstring)."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: config_path)

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="", default_temperature=0.5)

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/temperature 0.5")
        await pilot.pause()
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert data["default_temperature"] == 0.5

        screen._handle_command("/temperature off")
        await pilot.pause()

    assert settings.default_temperature is None
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "default_temperature" not in data


@pytest.mark.asyncio
async def test_temperature_command_with_no_argument_reports_current_value(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        screen._handle_command("/temperature")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "unset" in message_view._current_text


@pytest.mark.asyncio
async def test_temperature_command_rejects_non_numeric_and_negative_values(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        message_view = screen.query_one(MessageView)

        screen._handle_command("/temperature not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text
        assert settings.default_temperature is None

        screen._handle_command("/temperature -1")
        await pilot.pause()
        assert "0 or greater" in message_view._current_text
        assert settings.default_temperature is None


@pytest.mark.asyncio
async def test_budget_command_updates_settings_live_and_persists_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: config_path)

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/budget 5.00")
        await pilot.pause()

    assert settings.max_session_cost_usd == 5.00
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["max_session_cost_usd"] == 5.00


@pytest.mark.asyncio
async def test_budget_off_clears_settings_and_removes_the_persisted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same remove_config_keys reasoning as /temperature off - update_config_file
    alone can't express clearing a value back to unset."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: config_path)

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="", max_session_cost_usd=5.00)

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/budget 5.00")
        await pilot.pause()
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert data["max_session_cost_usd"] == 5.00

        screen._handle_command("/budget off")
        await pilot.pause()

    assert settings.max_session_cost_usd is None
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "max_session_cost_usd" not in data


@pytest.mark.asyncio
async def test_budget_command_with_no_argument_reports_current_value_and_spend(
    tmp_path: Path,
):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    session.cost.session_total_usd = 1.2345
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        screen._handle_command("/budget")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "unset" in message_view._current_text
        assert "$1.2345" in message_view._current_text


@pytest.mark.asyncio
async def test_budget_command_rejects_non_numeric_and_non_positive_values(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="fake-model", gateway_base_url="")
    settings = Settings(gateway_base_url="", gateway_api_key="")

    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        from pcli.tui.widgets.message_view import MessageView

        message_view = screen.query_one(MessageView)

        screen._handle_command("/budget not-a-number")
        await pilot.pause()
        assert "valid number" in message_view._current_text
        assert settings.max_session_cost_usd is None

        screen._handle_command("/budget 0")
        await pilot.pause()
        assert "greater than 0" in message_view._current_text
        assert settings.max_session_cost_usd is None
