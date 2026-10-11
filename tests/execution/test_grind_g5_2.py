"""Grind review of cdf33a1 (PR #21, claude/grind-review-cdf33a1.md), G5-2 (LOW): two
alarms each asked Andre to `book_by_hand` the same unrecorded round trip.

With no position in the ledger (its write failed at the fill), reconcile alarms
"flattening ... that the ledger never recorded ... Book it with Ledger.book_by_hand"
(no id), and when that flatten's price is late, CHECK THE FILL said "if the system
cannot book it, book it with Ledger.book_by_hand as reconcile-...". Followed both, the
round trip's P&L is booked twice (daily loss, weekly loss and drawdown, the kill
switch's inputs, are wrong). And with a stray contract held while a position is open,
the hold is under the open position: "if the system cannot book it, book it by hand"
told Andre to book a position that may still be held (closing it in the ledger).

Now: one instruction. The unrecorded-holding alarm says to book the round trip once,
under the reconcile order's id; CHECK THE FILL under an order's own id says that
booking it once as that id releases the hold and that it is the same round trip as
the "never recorded" alarm; under an open position it says never to book that
position by hand while it is held. Text only; nothing sent or booked changes."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.execution.conftest import SEND, Stack
from tests.execution.test_check_the_fill_restart import _restart, _run
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag
from tests.execution.test_unowned_unpriced_sell import (
    _check_the_fill,
    _reconcile_sells,
    _stray_rounds,
)
from tests.execution.test_unpriced_sell_guards import _EndsWith

SEC = timedelta(seconds=1)


def test_an_unrecorded_round_trip_is_booked_once_under_one_id(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    position = filled_position(stack)
    contract = position.contract_id
    stack.ledger._save(replace(stack.ledger._state, open_position_id=None))
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")
    lag.hold({contract: position.contracts})
    runner, _, _, alarms = _restart(stack, tmp_path)
    _run(stack, runner, SEND + 60 * SEC, 60)
    (sent,) = _reconcile_sells(lag, contract)
    messages = [a.message for a in alarms]
    unrecorded = [m for m in messages if m.startswith("flattening")]
    assert len(unrecorded) == 1
    assert "once" in unrecorded[0]
    assert "reconcile-" in unrecorded[0]  # under the reconcile order's id
    (told,) = _check_the_fill(messages)
    assert "if the system cannot book it" not in told
    assert f"once, with Ledger.book_by_hand as {sent}" in told
    assert "never recorded" in told  # the same round trip as that alarm
    assert "never book it twice" in told


def test_a_stray_hold_under_an_open_position_never_says_to_book_it_by_hand(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _ = _stray_rounds(stack, monkeypatch, 10, SEND + 60 * SEC)
    pid = stack.ledger.open_position_id
    assert pid is not None
    (told,) = _check_the_fill([a.message for a in stack.alarms])
    assert "if the system cannot book it" not in told
    assert f"book_by_hand as {pid}" not in told
    assert f"do not book {pid} by hand while it is still held" in told
    # A hold under the open position is not announced as one to book by hand (G5-1).
    assert not [a for a in stack.alarms if "hold is saved" in a.message]


# --- G5-1 (LOW, carried): a hold saved under the order's own id is never silent ------


def test_a_hold_saved_under_the_orders_own_id_is_told_at_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reviewer's probe G5-P1: no position in the ledger, reconcile's sell fills
    with its price pending for less than the wait. The hold is saved under the order's
    own id and only Andre's booking releases it, yet nothing told him until past the
    wait (and never, if the price came first). Now he is told when it is saved, once,
    with the one id to book it under; CHECK THE FILL is still kept for past the wait."""
    position = filled_position(stack)
    contract = position.contract_id
    stack.ledger._save(replace(stack.ledger._state, open_position_id=None))
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")
    lag.hold({contract: position.contracts})
    runner, ledger, _, alarms = _restart(stack, tmp_path)
    settle = stack.profiles.execution.reconcile.settle_seconds
    _run(stack, runner, SEND + 60 * SEC, 4 * (settle + 1))
    (sent,) = _reconcile_sells(lag, contract)
    assert [h.position_id for h in ledger.sold_unpriced] == [sent]
    messages = [a.message for a in alarms]
    assert not _check_the_fill(messages)  # inside the wait
    saved = [m for m in messages if "hold is saved" in m]
    assert len(saved) == 1
    assert f"Ledger.book_by_hand as {sent}, at" in saved[0]
    assert "once" in saved[0]
    assert "CHECK THE FILL" not in saved[0]
