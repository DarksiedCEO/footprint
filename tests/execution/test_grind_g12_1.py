"""Grind review of cd2a7c9 (PR #21, claude/grind-review-cd2a7c9.md), G12-1 (LOW, text):
the "ledger never recorded" alarm told Andre to book the round trip under the id of
the reconcile order that flattens it, "the alarm about its late price names it". When
that order's price is NOT late (the common case) no alarm named any id, so Andre was
told to use an id he was never given.

Now each reconcile order sent for a holding the ledger never recorded is named in its
alarm when the first one is sent (once per contract: a flatten repriced and sent again
is not named again), with the one rule: book the round trip once, under this id,
unless a later alarm names a hold under another reconcile order (then under that one).
Text only; nothing sent or booked changes."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import SEND, Stack
from tests.execution.test_check_the_fill_restart import _restart, _run
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag
from tests.execution.test_grind_g11_1 import _reconcile_sells_the_rest_price_pending
from tests.execution.test_unowned_unpriced_sell import _check_the_fill, _reconcile_sells

SEC = timedelta(seconds=1)


def test_an_unrecorded_holding_flattened_at_a_known_price_names_the_order_to_book(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    position = filled_position(stack)
    contract = position.contract_id
    stack.ledger._save(replace(stack.ledger._state, open_position_id=None))
    lag = Lag(stack.broker, monkeypatch)  # nothing hidden: its price arrives at once
    runner, _, _, alarms = _restart(stack, tmp_path)
    _run(stack, runner, SEND + 60 * SEC, 60)
    sent = _reconcile_sells(lag, contract)
    assert len(sent) == 1
    messages = [a.message for a in alarms]
    assert _check_the_fill(messages) == []  # the price was never late
    named = [m for m in messages if sent[0] in m]
    assert len(named) == 1, messages
    assert "no position in the ledger" in named[0]
    assert f"once, with Ledger.book_by_hand as {sent[0]}" in named[0]
    assert "never book it twice" in named[0]
    unrecorded = [m for m in messages if m.startswith("flattening")]
    assert len(unrecorded) == 1
    assert "late price" not in unrecorded[0]  # no longer points at an alarm that may never come
    assert "named when the first reconcile order for it is sent" in unrecorded[0]


def _named(stack: Stack) -> list[str]:
    return [
        a.message
        for a in stack.alarms
        if a.message.startswith("reconcile-") and "no position in the ledger" in a.message
    ]


def test_a_recorded_position_flattened_by_reconcile_is_never_named_for_booking_by_hand(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control (mutant M2): the position IS in the ledger; reconcile's sell of it is
    booked by the system, so no alarm tells Andre to book it by hand under that id."""
    position, lag, _, _ = _reconcile_sells_the_rest_price_pending(stack, monkeypatch)
    assert stack.ledger.open_position_id == position.position_id
    assert len(_reconcile_sells(lag, position.contract_id)) == 1  # reconcile did send
    assert _named(stack) == []


def test_a_new_unrecorded_holding_after_reconcile_agreed_is_named_again(
    stack: Stack,
) -> None:
    """Mutant M8: the once-per-contract memory is cleared when reconcile agrees, so a
    later holding with no position in the ledger (a new round trip) gets its own id."""
    settle = stack.profiles.execution.reconcile.settle_seconds
    t = DECISION_TIME
    for _ in range(2):
        stack.broker._positions[stack.contract] = 1  # held; unknown to the ledger
        for _ in range(4 * settle + 4):
            t += SEC
            stack.quote(160, 161, t)
            stack.manager.reconcile(None, t)
        assert stack.contract not in stack.broker._positions  # flattened
    named = _named(stack)
    assert len(named) == 2
    assert named[0].split()[0] != named[1].split()[0]  # two orders, two ids
