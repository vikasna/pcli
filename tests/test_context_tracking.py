import tomllib
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from pcli.cost.context import (
    ContextLimitTable,
    ContextUsage,
    current_context_usage,
    looks_like_context_ceiling,
    set_model_context_limit,
)
from pcli.llm.models import Usage
from pcli.session.models import Session, TurnCost
from pcli.tui.widgets.status_bar import StatusBar, format_token_count


class _BarApp(App):
    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")


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


def test_has_explicit_entry_true_for_exact_and_wildcard_matches():
    table = _fixture_limits()
    assert table.has_explicit_entry("claude-sonnet-exact") is True
    assert table.has_explicit_entry("gpt-4o-2024-08-06") is True


def test_has_explicit_entry_false_when_falling_back_to_the_generic_default():
    table = _fixture_limits()
    assert table.has_explicit_entry("totally-unknown-model") is False


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


def test_status_bar_renders_second_line_only_while_subagent_active():
    bar = StatusBar()
    bar.model = "gpt-4o"
    assert "\n" not in bar.render()

    bar.subagent_task = "investigate the bug"
    bar.subagent_tool_calls = 2
    bar.subagent_last_tool = "grep"
    text = bar.render()
    line1, line2 = text.split("\n")
    assert "model: gpt-4o" in line1
    assert "Subagent" in line2
    assert "investigate the bug" in line2
    assert "2 tool call(s)" in line2
    assert "grep" in line2

    bar.subagent_task = None
    assert "\n" not in bar.render()


# --- looks_like_context_ceiling ---
#
# Regression coverage for a real debugged session: a model with no
# ContextLimitTable entry (silently falling back to a generic 128000-token
# guess) produced three consecutive empty turns, each with total_tokens
# landing on (or within a few tokens of) 16384 - the model's real, much
# smaller window - despite prompt_tokens climbing every turn, while pcli's
# own fraction-based auto-compact check thought the session was only ~13%
# full and never triggered.


def _turn(index: int, prompt: int, completion: int) -> TurnCost:
    return TurnCost(
        turn_index=index,
        model="m",
        usage=Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion),
        cost_usd=0.0,
    )


def test_looks_like_context_ceiling_detects_plateaued_totals():
    session = Session(model="m")
    # Real numbers from the debugged session: prompt keeps climbing but
    # total stays pinned near 16384 as completion_tokens shrinks to fit.
    session.cost.turns.append(_turn(0, 14984, 1377))  # total 16361, real content
    session.cost.turns.append(_turn(1, 15414, 970))  # total 16384, empty turn
    assert looks_like_context_ceiling(session) is True


def test_looks_like_context_ceiling_false_for_normal_variation():
    session = Session(model="m")
    # Ordinary growth: both prompt_tokens and total_tokens climb together.
    session.cost.turns.append(_turn(0, 2000, 1000))  # total 3000
    session.cost.turns.append(_turn(1, 3000, 1500))  # total 4500
    assert looks_like_context_ceiling(session) is False


def test_looks_like_context_ceiling_false_with_only_one_turn():
    session = Session(model="m")
    session.cost.turns.append(_turn(0, 16284, 100))
    assert looks_like_context_ceiling(session) is False  # nothing to compare against


def test_looks_like_context_ceiling_false_when_totals_are_small():
    session = Session(model="m")
    session.cost.turns.append(_turn(0, 300, 100))
    session.cost.turns.append(_turn(1, 400, 100))
    assert looks_like_context_ceiling(session) is False  # below the minimum floor


def test_looks_like_context_ceiling_false_for_empty_session():
    session = Session(model="m")
    assert looks_like_context_ceiling(session) is False


def test_looks_like_context_ceiling_false_when_prompt_did_not_grow():
    session = Session(model="m")
    session.cost.turns.append(_turn(0, 15414, 970))  # total 16384
    session.cost.turns.append(_turn(1, 15400, 984))  # prompt shrank (e.g. after /compact)
    assert looks_like_context_ceiling(session) is False


# --- set_model_context_limit ---


def test_set_model_context_limit_creates_file(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: path)

    set_model_context_limit("google/gemma-4-12b-qat", 16384)

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["models"]["google/gemma-4-12b-qat"] == 16384
    assert raw["default"]["limit"] == 128_000

    table = context_module.ContextLimitTable.load()
    assert table.lookup("google/gemma-4-12b-qat") == 16384


def test_set_model_context_limit_preserves_other_entries(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: path)
    path.write_text('[default]\nlimit = 5000\n\n[models]\n"other-model" = 8192\n', encoding="utf-8")

    set_model_context_limit("my-model", 16384)

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["models"]["other-model"] == 8192  # untouched
    assert raw["models"]["my-model"] == 16384
    assert raw["default"]["limit"] == 5000  # untouched


def test_set_model_context_limit_overwrites_existing_entry_for_same_model(tmp_path: Path, monkeypatch):
    import pcli.cost.context as context_module

    path = tmp_path / "context_limits.toml"
    monkeypatch.setattr(context_module, "context_limits_file", lambda: path)

    set_model_context_limit("my-model", 8192)
    set_model_context_limit("my-model", 32768)

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["models"]["my-model"] == 32768


@pytest.mark.asyncio
async def test_status_bar_height_grows_and_shrinks_with_subagent_activity():
    app = _BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        await pilot.pause()
        assert int(bar.styles.height.value) == 1

        bar.subagent_task = "investigate the bug"
        await pilot.pause()
        assert int(bar.styles.height.value) == 2

        bar.subagent_task = None
        await pilot.pause()
        assert int(bar.styles.height.value) == 1
