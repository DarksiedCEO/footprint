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

from tests.execution.conftest import SEND, Stack
from tests.execution.test_check_the_fill_restart import _restart, _run
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag
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
