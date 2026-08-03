import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pcli.cli import app
from pcli.config import settings as settings_module

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _reset_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_settings() reuses its cached Settings when called with no
    # overrides (e.g. --local-api alone) — without resetting this between
    # tests, an earlier test's gateway_base_url leaks into a later one.
    monkeypatch.setattr(settings_module, "_settings", None)


def test_cli_flags_persist_gateway_and_model_but_not_api_key(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PCLI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PCLI_MODEL", raising=False)

    # "cost report" is a real subcommand, so the callback runs (persisting
    # the flags) without falling through to launching the full TUI.
    result = runner.invoke(
        app,
        [
            "--gateway-url",
            "http://localhost:1234/v1",
            "--model",
            "llama-3",
            "--api-key",
            "super-secret",
            "cost",
            "report",
        ],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["gateway_base_url"] == "http://localhost:1234/v1"
    assert data["default_model"] == "llama-3"
    assert "gateway_api_key" not in data


def test_cli_without_flags_does_not_touch_config(isolated_config: Path):
    result = runner.invoke(app, ["cost", "report"])

    assert result.exit_code == 0, result.output
    assert not isolated_config.exists()


def test_cli_artifact_threshold_flag_persists_and_overrides(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PCLI_ARTIFACT_THRESHOLD_CHARS", raising=False)

    result = runner.invoke(app, ["--artifact-threshold", "8000", "cost", "report"])

    assert result.exit_code == 0, result.output
    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["artifact_threshold_chars"] == 8000

    from pcli.config.settings import Settings

    assert Settings().artifact_threshold_chars == 8000


def test_cli_local_api_flag_pairs_with_the_active_gateway(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PCLI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PCLI_LOCAL_API_GATEWAYS", raising=False)

    result = runner.invoke(
        app, ["--gateway-url", "http://localhost:1234/v1", "--local-api", "cost", "report"]
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["local_api_gateways"] == ["http://localhost:1234/v1"]

    from pcli.config.settings import Settings

    settings = Settings(gateway_base_url="http://localhost:1234/v1")
    assert settings.is_local_api() is True
    # A different gateway is unaffected.
    assert Settings(gateway_base_url="http://other.test/v1").is_local_api() is False


def test_cli_local_api_flag_without_a_gateway_warns_and_does_not_crash(isolated_config: Path):
    result = runner.invoke(app, ["--local-api", "cost", "report"])

    assert result.exit_code == 0, result.output
    assert "needs a gateway to pair with" in result.output
    assert not isolated_config.exists()


def test_cli_local_api_flag_reuses_already_persisted_gateway(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PCLI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PCLI_LOCAL_API_GATEWAYS", raising=False)

    # First run sets the gateway (no --local-api yet).
    runner.invoke(app, ["--gateway-url", "http://localhost:1234/v1", "cost", "report"])
    # Second run enables local-api mode without repeating --gateway-url.
    result = runner.invoke(app, ["--local-api", "cost", "report"])

    assert result.exit_code == 0, result.output
    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["local_api_gateways"] == ["http://localhost:1234/v1"]
