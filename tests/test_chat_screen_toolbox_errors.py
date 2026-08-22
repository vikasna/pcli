"""Coverage for a gap found while auditing error paths: ToolboxManager.discover
calls the gateway to synthesize tool schemas when there's no curated plugin
(tools/toolbox/manager.py's synthesize_tools), so it can raise GatewayError
same as any other gateway call - but _toolbox_discover only ever caught
ToolboxDiscoveryError, so a gateway failure (timeout, 401, ...) during
/toolbox discover crashed the worker silently with nothing shown to the
user."""

from pathlib import Path

import pytest
from textual.app import App

from pcli.config.settings import Settings
from pcli.llm.errors import GatewayError
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path) -> ChatScreen:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )
    return ChatScreen(settings, session=session, store=store)


@pytest.mark.asyncio
async def test_toolbox_discover_gateway_error_is_shown_not_crashed(tmp_path: Path):
    screen = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._toolbox_manager is not None

        async def _raise_gateway_error(*_args, **_kwargs):
            raise GatewayError(
                "Read timed out on the gateway. Set a larger request_timeout_s (e.g. 600) via "
                "PCLI_REQUEST_TIMEOUT_S or config.toml.",
                retryable=True,
            )

        screen._toolbox_manager.discover = _raise_gateway_error

        screen._handle_command("/toolbox discover somepkg")
        for _ in range(5):
            await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "Discovery failed" in message_view._current_text
        assert "request_timeout_s" in message_view._current_text
