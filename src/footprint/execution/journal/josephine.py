"""Josephine (journal): an append-only record of every decision and every trade.

Three JSON-lines files:
- decisions: why the system did or did not trade at each decision point (every
  gate, read, score, contract, size and risk veto);
- trades: each closed trade's full fingerprint (money, birth story, exit story);
- plays: the day's play caller label and its reason, once per trading day (from the
  decision that fixed it).
Each record is written and fsynced before the call returns. Records are never
rewritten; the journal is the evidence the intelligence layer and Andre audit.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from footprint.decision.pipeline import Decision
from footprint.execution.executor.diego import Position
from footprint.execution.management.malik import Closed


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in asdict(value).items() if not k.startswith("_")}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


class Journal:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._decisions = directory / "decisions.jsonl"
        self._trades = directory / "trades.jsonl"
        self._plays = directory / "plays.jsonl"

    @property
    def agent_id(self) -> str:
        return "journal"

    def record_decision(self, decision: Decision) -> None:
        play = decision.play
        if play is not None and play.first:
            self._append(
                self._plays,
                {
                    "day": play.day,
                    "as_of": decision.as_of,
                    "regime": play.regime,
                    "cause": play.cause,
                    "entries_allowed": play.entries_allowed,
                    "reason": play.reason,
                    "label": play.label,
                },
            )
        verdict = decision.verdict
        self._append(
            self._decisions,
            {
                "as_of": decision.as_of,
                "fired": decision.fired,
                "reasons": decision.reasons(),
                "precheck": decision.precheck,
                "data_error": decision.data_error,
                "direction": verdict.direction if verdict else None,
                "score": verdict.score if verdict else None,
                "gates": verdict.gates if verdict else (),
                "reads": verdict.reads if verdict else (),
                "contract": decision.contract,
                "plan": decision.plan,
                "theta": decision.theta,
                "risk": decision.risk,
            },
        )

    def record_trade(self, position: Position, closed: Closed, decision: Decision) -> None:
        verdict = decision.verdict
        self._append(
            self._trades,
            {
                "position_id": position.position_id,
                "contract_id": position.contract_id,
                "direction": position.direction,
                "contracts": position.contracts,
                "entry_price": position.entry_price,
                "entry_ts": position.entry_ts,
                "exit_price": closed.exit_price,
                "exit_ts": closed.exit_ts,
                "hold_seconds": (closed.exit_ts - position.entry_ts).total_seconds(),
                "exit_reason": closed.reason,
                "net_pnl": closed.net_pnl,
                "session": position.session,
                "birth": {
                    "score": verdict.score if verdict else None,
                    "gates": verdict.gates if verdict else (),
                    "reads": verdict.reads if verdict else (),
                    "plan": decision.plan,
                },
                "events": position.events,
            },
        )

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(_plain(record), sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def decisions(self) -> list[dict[str, Any]]:
        return self._read(self._decisions)

    def trades(self) -> list[dict[str, Any]]:
        return self._read(self._trades)

    def plays(self) -> list[dict[str, Any]]:
        return self._read(self._plays)

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
