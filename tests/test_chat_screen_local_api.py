from pathlib import Path

import pytest

from pcli.config.settings import Settings
from pcli.cost import pricing_table as pricing_table_module
from pcli.llm.models import Usage
from pcli.permissions import guardrails as guardrails_module
from pcli.session.models import Session
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen


@pytest.fixture(autouse=True)
def _isolated_config_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GuardrailsConfig.load() and PricingTable.load() otherwise read (and, for
    # guardrails, create) the real machine's guardrails.toml/pricing.toml —
    # isolate both so these tests don't depend on or mutate real files.
    monkeypatch.setattr(guardrails_module, "guardrails_file", lambda: tmp_path / "guardrails.toml")
    monkeypatch.setattr(pricing_table_module, "pricing_file", lambda: tmp_path / "pricing.toml")


def test_local_api_mode_uncaps_guardrails_iterations_and_zeroes_cost(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="llama-3-fake", gateway_base_url="http://localhost:1234/v1")
    settings = Settings(
        gateway_base_url="http://localhost:1234/v1",
        gateway_api_key="",
        local_api_gateways=["http://localhost:1234/v1"],
    )

    screen = ChatScreen(settings, session=session, store=store)

    assert settings.is_local_api() is True
    assert screen._permission_manager.guardrails.max_tool_calls_per_turn == 0
    assert screen._permission_manager.guardrails.max_tool_calls_per_minute == 0
    assert screen._effective_max_tool_iterations() is None

    # "llama-3-fake" would match the builtin "llama-3*" pricing pattern
    # (0.20 / 0.20 per 1M tokens) and show a nonzero cost if the free
    # pricing table weren't in effect for a local-api session.
    turn = screen._cost_tracker.record_turn(
        "llama-3-fake",
        Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    assert turn.cost_usd == 0.0
    assert session.cost.session_total_usd == 0.0


def test_non_local_api_gateway_keeps_limits_and_real_pricing(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = Session(model="llama-3-fake", gateway_base_url="http://elsewhere.test/v1")
    settings = Settings(
        gateway_base_url="http://elsewhere.test/v1",
        gateway_api_key="k",
        # Marked local-api for a *different* gateway only.
        local_api_gateways=["http://localhost:1234/v1"],
    )

    screen = ChatScreen(settings, session=session, store=store)

    assert settings.is_local_api() is False
    assert screen._permission_manager.guardrails.max_tool_calls_per_turn == 25
    assert screen._permission_manager.guardrails.max_tool_calls_per_minute == 60
    assert screen._effective_max_tool_iterations() == settings.max_tool_iterations

    turn = screen._cost_tracker.record_turn(
        "llama-3-fake",
        Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    assert turn.cost_usd > 0.0  # real pricing table applies
