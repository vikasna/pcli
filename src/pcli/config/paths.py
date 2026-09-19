"""Cross-platform config/data/cache directory resolution for pcli."""

from pathlib import Path

from platformdirs import PlatformDirs

_dirs = PlatformDirs(appname="pcli", appauthor=False)


def config_dir() -> Path:
    path = Path(_dirs.user_config_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    path = Path(_dirs.user_data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = Path(_dirs.user_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir() -> Path:
    path = data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def toolbox_dir() -> Path:
    path = data_dir() / "toolbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent_tools_file() -> Path:
    return data_dir() / "agent_tools.json"


def memory_file() -> Path:
    # JSON, not TOML: written to at runtime (new entries appended, old ones
    # evicted), same reasoning as permissions_file().
    return data_dir() / "memory.json"


def config_file() -> Path:
    return config_dir() / "config.toml"


def pricing_file() -> Path:
    return config_dir() / "pricing.toml"


def context_limits_file() -> Path:
    return config_dir() / "context_limits.toml"


def guardrails_file() -> Path:
    return config_dir() / "guardrails.toml"


def permissions_file() -> Path:
    # JSON, not TOML: this file is written to at runtime (new "always allow"
    # grants are appended), and the stdlib has no TOML writer.
    return config_dir() / "permissions.json"


def cost_ledger_file() -> Path:
    return data_dir() / "cost_ledger.jsonl"
