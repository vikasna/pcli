from pathlib import Path

from pcli.cost.context import ContextLimitTable, ContextUsage, current_context_usage
from pcli.llm.models import Usage
from pcli.session.models import Session, TurnCost
from pcli.tui.widgets.status_bar import StatusBar, format_token_count


def _fixture_limits() -> ContextLimitTable:
    return ContextLimitTable(
        entries={"gpt-4o*": 128_000, "claude-sonnet-exact": 200_000}, default=32_000
    )


def test_context_limit_table_exact_and_wildcard_and_default():
    table = _fixture_limits()
    assert table.lookup("claude-sonnet-exact") == 200_000  # exact match wins
    assert table.lookup("gpt-4o-2024-08-06") == 128_000  # wildcard match
    assert table.lookup("totally-unknown-model") == 32_000  # falls back to default


def test_context_limit_table_load_reads_overrides(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    monkeypatch.setattr(context_module, "context_limits_file", lambda: tmp_path / "context_limits.toml")
    (tmp_path / "context_limits.toml").write_text(
        '[models]\n"my-model*" = 999000\n\n[default]\nlimit = 5000\n', encoding="utf-8"
    )
    table = context_module.ContextLimitTable.load()
    assert table.lookup("my-model-v2") == 999_000
    assert table.lookup("anything-else") == 5000


def test_current_context_usage_uses_last_turn_only():
    session = Session(model="gpt-4o")
    session.cost.turns.append(
        TurnCost(turn_index=0, model="gpt-4o", usage=Usage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100), cost_usd=0.0)
    )
    session.cost.turns.append(
        TurnCost(turn_index=1, model="gpt-4o", usage=Usage(prompt_tokens=5000, completion_tokens=200, total_tokens=5200), cost_usd=0.0)
    )
    usage = current_context_usage(session, limit_table=_fixture_limits())
    assert usage.used_tokens == 5200  # the LAST turn's total, not a cumulative sum
    assert usage.limit_tokens == 128_000


def test_current_context_usage_empty_session():
    session = Session(model="gpt-4o")
    usage = current_context_usage(session, limit_table=_fixture_limits())
    assert usage.used_tokens == 0
    assert usage.limit_tokens == 128_000


def test_context_usage_fraction():
    usage = ContextUsage(used_tokens=64_000, limit_tokens=128_000)
    assert usage.fraction == 0.5
    assert ContextUsage(used_tokens=0, limit_tokens=0).fraction == 0.0  # no div-by-zero


def test_format_token_count():
    assert format_token_count(999) == "999"
    assert format_token_count(1_500) == "1.5k"
    assert format_token_count(128_000) == "128.0k"
    assert format_token_count(2_500_000) == "2.5m"


def test_status_bar_render_includes_context_when_limit_set():
    bar = StatusBar()
    bar.model = "gpt-4o"
    bar.session_cost_usd = 0.1234
    bar.total_tokens = 5000
    bar.context_used_tokens = 12_800
    bar.context_limit_tokens = 128_000
    text = bar.render()
    assert "ctx: 12.8k/128.0k (10%)" in text
    assert "model: gpt-4o" in text
    assert "cost: $0.1234" in text


def test_status_bar_render_omits_context_when_no_limit():
    bar = StatusBar()
    text = bar.render()
    assert "ctx:" not in text
