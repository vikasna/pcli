"""Regression coverage for the app's actual stylesheet (styles/pcli.tcss).

Every other TUI test mounts ChatScreen on a bare Textual App directly
(`class _HostApp(App): ...`), which never loads CSS_PATH — so a genuinely
invalid rule in pcli.tcss (e.g. `border-left: thick $text-muted;`, which
Textual rejects because $text-muted is an "auto"-computed contrast color,
not a concrete one the border-color parser accepts) sailed through the
entire test suite and only broke at real startup. This mounts the real
PcliApp, CSS_PATH and all, so a stylesheet parse error fails here instead."""

import pytest

from pcli.config.settings import Settings
from pcli.tui.app import PcliApp


def _settings() -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )


@pytest.mark.asyncio
async def test_pcli_app_stylesheet_parses_and_mounts():
    app = PcliApp(_settings())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen is not None
