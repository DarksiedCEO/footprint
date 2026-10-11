"""Grind review of 0b79482 (PR #21), test gaps G3-1 and G3-2 (LOW; the code is right,
nothing pinned it).

G3-1: the durable D40 hold (a sell the broker reported FILLED with its price pending)
counts by contract, also when the ledger never recorded the position (its write
failed at the fill). Scoping holds to the ledger's open position (reviewer mutant R5)
lets a restarted runner sell beside the filled sell while the report lags: a short.

G3-2: a position the ledger never recorded, sold with its price pending and then
priced, releases its hold when it is closed. Leaving the hold (reviewer mutant R1)
would later stop reconcile from flattening, and cancel the stop of, a new position on
the same contract."""

from dataclasses import replace
from pathlib import Path

import pytest

from footprint.execution.orders import SimulatedBroker
from tests.execution.conftest import Stack
from tests.execution.test_check_the_fill_restart import (
    _many,
    _reconcile_sells,
    _restart,
    _run,
)
from tests.execution.test_fill_price_pending import (
    SEC,
    Lag,
    _exit_filled_unpriced,
    _wait,
)


def _unrecorded(stack: Stack) -> None:
    """As if the ledger's write failed at the entry's fill."""
    stack.ledger._save(replace(stack.ledger._state, open_position_id=None))
    assert stack.ledger.open_position_id is None


def test_a_restart_never_sells_beside_a_held_sale_of_a_position_the_ledger_never_recorded(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)  # the report still shows it held
    assert [h.contract_id for h in stack.ledger.sold_unpriced] == [position.contract_id]
    _unrecorded(stack)
    runner, ledger, halt, alarms = _restart(stack, tmp_path)
    _run(stack, runner, t + 3 * _wait(stack), _many(stack))
    assert _reconcile_sells(lag) == []  # never sold beside the filled sell
    assert lag.sells() == [f"{position.position_id}-exit-1"]  # the one real sale only
    assert SimulatedBroker.positions(stack.broker) == {}  # never short at the broker
    assert halt.tripped
    check = [a.message for a in alarms if "CHECK THE FILL" in a.message]
    assert len(check) == 1
    assert "before this restart" in check[0]
    assert [h.contract_id for h in ledger.sold_unpriced] == [position.contract_id]  # kept


def test_closing_a_position_the_ledger_never_recorded_releases_its_hold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    assert [h.position_id for h in stack.ledger.sold_unpriced] == [position.position_id]
    _unrecorded(stack)
    lag.reveal()  # the price arrives: the sale is confirmed at its real price
    stack.quote(150, 151, t + SEC)
    closed = stack.manager.flatten(position, t + SEC)
    assert closed is not None
    assert any("closed but the ledger never recorded it" in a.message for a in stack.alarms)
    assert stack.ledger.sold_unpriced == ()  # released with the sale
    assert not any("could not release its hold" in a.message for a in stack.alarms)
