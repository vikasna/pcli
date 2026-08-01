"""Per-turn/session/global cost aggregation."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pcli.config.paths import cost_ledger_file
from pcli.cost.pricing_table import PricingTable
from pcli.llm.models import Usage
from pcli.session.models import Session, TurnCost


class CostTracker:
    def __init__(
        self,
        session: Session,
        *,
        pricing_table: PricingTable | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        self._session = session
        self._pricing = pricing_table or PricingTable.load()
        self._ledger_path = ledger_path or cost_ledger_file()

    def record_turn(self, model: str, usage: Usage) -> TurnCost:
        cost_usd = self._pricing.cost_usd(
            model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
        turn = TurnCost(
            turn_index=len(self._session.cost.turns),
            model=model,
            usage=usage,
            cost_usd=cost_usd,
            estimated=usage.estimated,
        )
        self._session.cost.turns.append(turn)
        self._session.cost.session_total_usd += cost_usd
        self._session.cost.total_tokens += usage.total_tokens
        self._append_to_global_ledger(turn)
        return turn

    def _append_to_global_ledger(self, turn: TurnCost) -> None:
        record = {
            "session_id": self._session.id,
            "turn_index": turn.turn_index,
            "model": turn.model,
            "prompt_tokens": turn.usage.prompt_tokens,
            "completion_tokens": turn.usage.completion_tokens,
            "total_tokens": turn.usage.total_tokens,
            "cost_usd": turn.cost_usd,
            "estimated": turn.estimated,
            "created_at": turn.created_at.isoformat(),
        }
        path = self._ledger_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def global_cost_report(since: datetime | None = None, *, ledger_path: Path | None = None) -> dict:
    path = ledger_path or cost_ledger_file()
    total_cost = 0.0
    total_tokens = 0
    turn_count = 0
    by_model: dict[str, float] = defaultdict(float)

    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                created_at = datetime.fromisoformat(record["created_at"])
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                if since is not None and created_at < since:
                    continue
                total_cost += record["cost_usd"]
                total_tokens += record["total_tokens"]
                turn_count += 1
                by_model[record["model"]] += record["cost_usd"]

    return {
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "turn_count": turn_count,
        "by_model": dict(by_model),
    }
