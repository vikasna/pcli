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
from pcli.tui.themes import VIM_THEMES


def _settings(**overrides) -> Settings:
    return Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        **overrides,
    )


@pytest.mark.asyncio
async def test_pcli_app_stylesheet_parses_and_mounts():
    app = PcliApp(_settings())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen is not None


@pytest.mark.asyncio
async def test_pcli_app_registers_the_vim_themes():
    app = PcliApp(_settings())
    async with app.run_test() as pilot:
        await pilot.pause()
        for theme in VIM_THEMES:
            assert theme.name in app.available_themes


@pytest.mark.asyncio
async def test_pcli_app_applies_ui_theme_from_settings():
    app = PcliApp(_settings(ui_theme="vim-elflord"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "vim-elflord"


@pytest.mark.asyncio
async def test_pcli_app_ignores_an_unknown_saved_theme_instead_of_failing_startup():
    """Regression coverage: settings.ui_theme is free-text (hand-edited
    config.toml, or a stale value from a pcli version whose theme set has
    since changed) - applying it blindly would raise straight out of
    on_mount. An unknown name must be a silent no-op, not a startup crash."""
    app = PcliApp(_settings(ui_theme="not-a-real-theme"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen is not None
        assert app.theme != "not-a-real-theme"
