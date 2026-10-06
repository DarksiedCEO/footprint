"""A restart never decides a minute twice: the newest bar decided on is durable."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from footprint.core.models import Bar
from footprint.execution.ledger import Ledger
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

MINUTE = timedelta(minutes=1)
T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def reopened(stack: Stack) -> Ledger:
    """The ledger as a new process would open it, from the same file."""
    return Ledger(stack.profiles.risk, stack.profiles.instrument, stack.ledger._store)


def test_the_newest_decided_bar_survives_a_restart(stack: Stack) -> None:
    assert stack.ledger.last_decided is None
    stack.ledger.mark_decided(T)
    assert reopened(stack).last_decided == T


def test_it_never_moves_backwards(stack: Stack) -> None:
    stack.ledger.mark_decided(T)
    stack.ledger.mark_decided(T - MINUTE)  # an older bar (a late or replayed feed)
    assert stack.ledger.last_decided == T
    assert reopened(stack).last_decided == T


def test_a_naive_time_is_refused(stack: Stack) -> None:
    with pytest.raises(ValueError, match="UTC"):
        stack.ledger.mark_decided(datetime(2026, 9, 15, 14, 0))  # noqa: DTZ001 (the point)


def test_a_state_file_from_before_this_field_still_opens(stack: Stack, tmp_path: Path) -> None:
    path = stack.ledger._store.path
    raw = json.loads(path.read_text())
    raw.pop("last_decided", None)
    path.write_text(json.dumps(raw))
    assert reopened(stack).last_decided is None


def start(runner: Any) -> None:
    """The first step reconciles; the next decides on the newest completed bar."""
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + timedelta(seconds=1))


def count_decisions(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    seen: list[datetime] = []
    real = stack.pipeline.decide

    def counting(*args: Any) -> Any:
        seen.append(args[-1])
        return real(*args)

    monkeypatch.setattr(stack.pipeline, "decide", counting)
    return seen


def test_a_restarted_runner_does_not_decide_the_same_minute_again(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decided = count_decisions(stack, monkeypatch)
    runner, _, _ = runner_for(stack, bars, tmp_path / "first")
    start(runner)
    assert len(decided) == 1
    newest = runner.last_decided
    assert newest is not None
    assert stack.ledger.last_decided == newest  # saved before anything is acted on
    # The process dies and starts again within the same minute; Andre clears it.
    stack.ledger = reopened(stack)
    restarted, _, _ = runner_for(stack, bars, tmp_path / "second")
    assert restarted.last_decided == newest
    for second in range(20, 40):  # reconcile, then steps through the rest of the minute
        restarted.step(DECISION_TIME + timedelta(seconds=second))
    assert len(decided) == 1  # that minute is not decided again
    bars.append(replace(bars[-1], ts=bars[-1].ts + MINUTE))  # a new minute's bar closes
    restarted.step(DECISION_TIME + MINUTE)
    assert len(decided) == 2  # and that one is decided


def test_the_decided_minute_is_saved_before_the_decision_is_made(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_at_decision: list[datetime | None] = []
    real = stack.pipeline.decide

    def watching(*args: Any) -> Any:
        saved_at_decision.append(reopened(stack).last_decided)
        return real(*args)

    monkeypatch.setattr(stack.pipeline, "decide", watching)
    runner, _, _ = runner_for(stack, bars, tmp_path)
    start(runner)
    assert saved_at_decision == [runner.last_decided]  # durable first: a crash inside the
    # decision (or the send it leads to) can never make a restart decide that minute again


def test_a_minute_that_cannot_be_saved_is_not_decided(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decided = count_decisions(stack, monkeypatch)

    def disk_full(*args: Any) -> None:
        raise OSError("disk full")

    runner, _, alarms = runner_for(stack, bars, tmp_path)
    monkeypatch.setattr(stack.ledger._store, "save", disk_full)
    start(runner)
    assert decided == []  # never decide what a restart could decide again
    assert stack.halt.tripped
    assert any("disk full" in a.message for a in alarms)
