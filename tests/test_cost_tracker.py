import json
from pathlib import Path

from pcli.cost.pricing_table import ModelPricing, PricingTable
from pcli.cost.tracker import CostTracker, global_cost_report
from pcli.llm.models import Usage
from pcli.session.models import Session, TurnCost


def _fixture_pricing() -> PricingTable:
    return PricingTable(
        entries={"fake-model": ModelPricing(input_per_1m=2.0, output_per_1m=4.0)},
        default=ModelPricing(input_per_1m=0.0, output_per_1m=0.0),
    )


def test_pricing_table_lookup_exact_and_wildcard():
    table = PricingTable(
        entries={
            "gpt-4o*": ModelPricing(input_per_1m=2.5, output_per_1m=10.0),
            "gpt-4o-mini": ModelPricing(input_per_1m=0.15, output_per_1m=0.6),
        },
        default=ModelPricing(),
    )
    assert table.lookup("gpt-4o-mini").input_per_1m == 0.15  # exact match wins over wildcard
    assert table.lookup("gpt-4o-2024-08-06").input_per_1m == 2.5  # wildcard match
    assert table.lookup("totally-unknown-model").input_per_1m == 0.0  # falls back to default


def test_cost_tracker_aggregates_turns(tmp_path: Path):
    session = Session(model="fake-model")
    ledger_path = tmp_path / "ledger.jsonl"
    tracker = CostTracker(session, pricing_table=_fixture_pricing(), ledger_path=ledger_path)

    turn1 = tracker.record_turn(
        "fake-model", Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    )
    turn2 = tracker.record_turn(
        "fake-model", Usage(prompt_tokens=0, completion_tokens=1_000_000, total_tokens=1_000_000)
    )

    assert turn1.cost_usd == 2.0
    assert turn2.cost_usd == 4.0
    assert session.cost.session_total_usd == 6.0
    assert session.cost.total_tokens == 2_000_000
    assert len(session.cost.turns) == 2
    assert turn1.turn_index == 0
    assert turn2.turn_index == 1

    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert record["session_id"] == session.id
    assert record["cost_usd"] == 2.0


def test_cost_tracker_defaults_source_to_main(tmp_path: Path):
    session = Session(model="fake-model")
    tracker = CostTracker(
        session, pricing_table=_fixture_pricing(), ledger_path=tmp_path / "ledger.jsonl"
    )
    turn = tracker.record_turn(
        "fake-model", Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    )
    assert turn.source == "main"


def test_cost_tracker_records_subagent_and_compaction_sources(tmp_path: Path):
    """subagent/compaction spend still counts toward the session total (real
    money spent either way) - only the *source* tag differs, used by
    cost/context.py to tell a main-conversation entry apart from one that
    reflects a completely different, unrelated conversation's size."""
    session = Session(model="fake-model")
    tracker = CostTracker(
        session, pricing_table=_fixture_pricing(), ledger_path=tmp_path / "ledger.jsonl"
    )
    usage = Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)

    subagent_turn = tracker.record_turn("fake-model", usage, source="subagent")
    compaction_turn = tracker.record_turn("fake-model", usage, source="compaction")

    assert subagent_turn.source == "subagent"
    assert compaction_turn.source == "compaction"
    # Still real spend - both count toward the aggregate totals.
    assert session.cost.total_tokens == 300
    assert session.cost.session_total_usd > 0


def test_old_turn_cost_without_source_field_still_validates_as_main():
    """A session persisted before TurnCost.source existed must still load
    fine, and every one of its entries is implicitly a main-conversation
    entry (that's all that existed before subagent/compaction tagging)."""
    raw = TurnCost(
        turn_index=0, model="fake-model", usage=Usage(total_tokens=100), cost_usd=0.0
    ).model_dump(mode="json")
    del raw["source"]
    restored = TurnCost.model_validate(raw)
    assert restored.source == "main"
    json.dumps(raw)  # sanity: still valid JSON without the field present


def test_cost_tracker_marks_estimated_usage(tmp_path: Path):
    session = Session(model="fake-model")
    tracker = CostTracker(
        session, pricing_table=_fixture_pricing(), ledger_path=tmp_path / "ledger.jsonl"
    )
    turn = tracker.record_turn(
        "fake-model",
        Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150, estimated=True),
    )
    assert turn.estimated is True


def test_global_cost_report_aggregates_across_sessions(tmp_path: Path):
    ledger_path = tmp_path / "ledger.jsonl"
    pricing = _fixture_pricing()

    session_a = Session(model="fake-model")
    CostTracker(session_a, pricing_table=pricing, ledger_path=ledger_path).record_turn(
        "fake-model", Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    )
    session_b = Session(model="fake-model")
    CostTracker(session_b, pricing_table=pricing, ledger_path=ledger_path).record_turn(
        "fake-model", Usage(prompt_tokens=0, completion_tokens=1_000_000, total_tokens=1_000_000)
    )

    report = global_cost_report(ledger_path=ledger_path)
    assert report["turn_count"] == 2
    assert report["total_cost_usd"] == 6.0
    assert report["total_tokens"] == 2_000_000
    assert report["by_model"]["fake-model"] == 6.0
