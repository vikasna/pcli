"""Coverage for ask_artifact's conditional registration: available only
when Settings.is_local_api() is true (see tui/screens/chat.py, right after
self._tool_registry = build_default_registry()) - the first tool in pcli
conditionally excluded based on settings, rather than always registered
like everything else. The tool's own handler behavior (small-artifact
short-circuit, the LLM-answered path, extra_usage) is covered separately in
test_artifact_tool.py."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path, *, local_api: bool) -> ChatScreen:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        local_api_gateways=["http://fake-gateway.test/v1"] if local_api else [],
    )
    return ChatScreen(settings, session=session, store=store)


@pytest.mark.asyncio
async def test_ask_artifact_is_registered_in_local_api_mode(tmp_path: Path):
    screen = _make_screen(tmp_path, local_api=True)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert screen._tool_registry is not None
        assert "ask_artifact" in screen._tool_registry
        assert "fetch_artifact" in screen._tool_registry  # unaffected, still present


@pytest.mark.asyncio
async def test_ask_artifact_is_absent_outside_local_api_mode(tmp_path: Path):
    screen = _make_screen(tmp_path, local_api=False)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert screen._tool_registry is not None
        assert "ask_artifact" not in screen._tool_registry
        assert "fetch_artifact" in screen._tool_registry  # unaffected, still present
