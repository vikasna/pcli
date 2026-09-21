"""Coverage for `pcli telegram`'s own CLI-level wiring: config validation
and exit codes. The actual daemon (message queueing, permission
correlation, PTB handler registration) has its own dedicated test files
(test_telegram_daemon.py, test_telegram_permissions.py, test_telegram_bot.py)
- run_telegram_daemon itself is mocked out here so these tests only
exercise cli.py's own argument/config-check/exit-code wiring, matching
test_cli_run.py's established pattern for the same reason."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from pcli.cli import app
from pcli.config import settings as settings_module

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "_settings", None)


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("pcli.config.paths.data_dir", lambda: data_dir)


def _base_args(*extra: str) -> list[str]:
    return [
        "--gateway-url",
        "http://fake-gateway.test/v1",
        "--api-key",
        "test-key",
        "--model",
        "fake-model",
        "telegram",
        *extra,
    ]


def test_telegram_requires_a_configured_gateway(isolated_store: None):
    result = runner.invoke(app, ["telegram"])
    assert result.exit_code == 1
    assert "Gateway not configured" in result.output


def test_telegram_requires_bot_token_and_chat_id(isolated_store: None):
    result = runner.invoke(app, _base_args())
    assert result.exit_code == 1
    assert "telegram_bot_token and telegram_chat_id" in result.output


def test_telegram_requires_a_chat_id_even_with_a_token(
    isolated_store: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PCLI_TELEGRAM_BOT_TOKEN", "abc123")
    result = runner.invoke(app, _base_args())
    assert result.exit_code == 1
    assert "telegram_bot_token and telegram_chat_id" in result.output


def test_telegram_starts_the_daemon_when_fully_configured(
    isolated_store: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PCLI_TELEGRAM_BOT_TOKEN", "abc123")
    monkeypatch.setenv("PCLI_TELEGRAM_CHAT_ID", "555")

    async def _fake_run_telegram_daemon(settings, cwd, *, on_ready=lambda _s: None):
        on_ready("sess_fake")

    monkeypatch.setattr(
        "pcli.cli.run_telegram_daemon", AsyncMock(side_effect=_fake_run_telegram_daemon)
    )

    result = runner.invoke(app, _base_args())

    assert result.exit_code == 0
    assert "Listening for chat 555" in result.output
    assert "sess_fake" in result.output


def test_telegram_reports_startup_failure_cleanly(
    isolated_store: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PCLI_TELEGRAM_BOT_TOKEN", "abc123")
    monkeypatch.setenv("PCLI_TELEGRAM_CHAT_ID", "555")
    monkeypatch.setattr(
        "pcli.cli.run_telegram_daemon", AsyncMock(side_effect=RuntimeError("sandbox failed"))
    )

    result = runner.invoke(app, _base_args())

    assert result.exit_code == 1
    assert "Startup failed" in result.output
    assert "sandbox failed" in result.output
