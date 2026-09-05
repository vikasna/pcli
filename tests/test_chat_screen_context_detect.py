"""Coverage for ChatScreen._maybe_detect_context_limit, called from
on_mount right after the GatewayClient is built: skips entirely when a
context-limit entry already exists for the model or the feature is
disabled, persists+applies a successful detection immediately, and shows
a fallback notice pointing at /context-limit when nothing was detected."""

import tomllib
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView
from pcli.tui.widgets.status_bar import StatusBar


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path, *, model: str = "unrecognized-local-model", **settings_overrides):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model=model, gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model=model,
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, session


def _mock_all_native_probes_404() -> None:
    respx.get("http://fake-gateway.test/api/v0/models").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/props").mock(return_value=httpx.Response(404))
    respx.post("http://fake-gateway.test/api/show").mock(return_value=httpx.Response(404))
    respx.get("http://fake-gateway.test/model/info").mock(return_value=httpx.Response(404))


@pytest.mark.asyncio
@respx.mock
async def test_successful_detection_persists_and_updates_the_status_bar(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)

    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "unrecognized-local-model", "context_length": 32768}]}
        )
    )
    _mock_all_native_probes_404()

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected context limit" in message_view._current_text
        assert "32,768" in message_view._current_text

        status_bar = screen.query_one(StatusBar)
        assert status_bar.context_limit_tokens == 32768

        raw = tomllib.loads(limits_path.read_text(encoding="utf-8"))
        assert raw["models"]["unrecognized-local-model"] == 32768


@pytest.mark.asyncio
@respx.mock
async def test_failed_detection_shows_a_notice_pointing_at_context_limit_command(tmp_path: Path):
    respx.get("http://fake-gateway.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "unrecognized-local-model"}]})
    )
    _mock_all_native_probes_404()

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Couldn't auto-detect" in message_view._current_text
        assert "/context-limit" in message_view._current_text


@pytest.mark.asyncio
async def test_skips_probing_entirely_when_model_already_has_an_explicit_entry(tmp_path: Path):
    """gpt-4o* matches a builtin ContextLimitTable pattern - no network call
    should even be attempted (no respx mock registered at all; an
    unexpected request here would raise)."""
    screen, _session = _make_screen(tmp_path, model="gpt-4o-mini")
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text


@pytest.mark.asyncio
async def test_skips_entirely_when_disabled_via_settings(tmp_path: Path):
    screen, _session = _make_screen(tmp_path, context_limit_auto_detect_enabled=False)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_a_prior_manual_context_limit_setting_is_never_overwritten(tmp_path: Path, monkeypatch):
    """Regression guard: has_explicit_entry() must treat a manual
    /context-limit correction (or a previous auto-detection) as final -
    detection must not silently re-probe and clobber it with a different
    value on a later launch."""
    import pcli.cost.context as context_module

    limits_path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: limits_path)
    context_module.set_model_context_limit("unrecognized-local-model", 8000)

    screen, _session = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert "Auto-detected" not in message_view._current_text
        assert "Couldn't auto-detect" not in message_view._current_text
        assert screen._context_limit_table.lookup("unrecognized-local-model") == 8000
