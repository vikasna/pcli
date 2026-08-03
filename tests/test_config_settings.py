import tomllib
from pathlib import Path

import pytest

from pcli.config import settings as settings_module
from pcli.config.settings import Settings, update_config_file


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: path)
    return path


def test_update_config_file_writes_new_keys(isolated_config: Path):
    update_config_file(gateway_base_url="http://localhost:1234/v1", default_model="llama-3")

    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data == {"gateway_base_url": "http://localhost:1234/v1", "default_model": "llama-3"}


def test_update_config_file_merges_with_existing_content(isolated_config: Path):
    isolated_config.write_text('gateway_api_key = "secret"\nsandbox_backend = "docker"\n', encoding="utf-8")

    update_config_file(gateway_base_url="http://localhost:1234/v1")

    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data == {
        "gateway_api_key": "secret",
        "sandbox_backend": "docker",
        "gateway_base_url": "http://localhost:1234/v1",
    }


def test_update_config_file_skips_falsy_values(isolated_config: Path):
    update_config_file(gateway_base_url="http://a.test/v1", default_model="model-a")
    update_config_file(gateway_base_url=None, default_model="model-b")

    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    # gateway_base_url untouched (None was skipped, not written as blank).
    assert data == {"gateway_base_url": "http://a.test/v1", "default_model": "model-b"}


def test_update_config_file_escapes_special_characters(isolated_config: Path):
    update_config_file(default_model='weird "model" name\\path')

    data = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["default_model"] == 'weird "model" name\\path'


def test_settings_picks_up_persisted_gateway_and_model(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PCLI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PCLI_MODEL", raising=False)

    update_config_file(gateway_base_url="http://localhost:1234/v1", default_model="llama-3")

    settings = Settings()
    assert settings.gateway_base_url == "http://localhost:1234/v1"
    assert settings.default_model == "llama-3"
