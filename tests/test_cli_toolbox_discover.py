"""Coverage for a gap found while auditing error paths: `pcli toolbox
discover` only caught ToolboxDiscoveryError, but ToolboxManager.discover
calls the gateway to synthesize tool schemas when there's no curated
plugin, and that can fail with GatewayError same as any other gateway call
- previously uncaught here, so it crashed with a raw traceback instead of
a clean "Discovery failed: ..." message and exit code 1."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pcli.cli import app
from pcli.config import settings as settings_module
from pcli.llm.errors import GatewayError
from pcli.tools.toolbox import manager as manager_module

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _reset_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "_settings", None)


def test_toolbox_discover_reports_gateway_error_cleanly(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
):
    async def _raise_gateway_error(self, *_args, **_kwargs):
        raise GatewayError(
            "Read timed out on the gateway. Set a larger request_timeout_s (e.g. 600) via "
            "PCLI_REQUEST_TIMEOUT_S or config.toml.",
            retryable=True,
        )

    monkeypatch.setattr(manager_module.ToolboxManager, "discover", _raise_gateway_error)

    result = runner.invoke(
        app,
        [
            "--gateway-url",
            "http://fake-gateway.test/v1",
            "--api-key",
            "test-key",
            "toolbox",
            "discover",
            "somepkg",
        ],
    )

    assert result.exit_code == 1
    assert "Discovery failed" in result.output
    assert "request_timeout_s" in result.output
