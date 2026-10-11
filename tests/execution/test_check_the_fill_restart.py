"""Grind review G2-1 (MEDIUM): the D40 hold must survive a restart.

A sell of the position the broker reports FILLED with its price pending is the
position's sale. Before this fix the hold lived only in the manager's memory: a
restarted runner calls `reconcile(None)`, found nothing pending, and flattened what
the broker's (lagging) report still showed held, beside the known filled sell, which
can go short. The hold is now kept in the ledger's state file (the contract, the
filled order and its position) from the moment it is known. It is released only when
the sale is booked at its real price, or when Andre books the position by hand.

Also G2-2 (two reviewer mutants that survived): the durable latch is retried every
overdue round, and a stray holding on another contract beside a hold is still
flattened."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.calendar import SessionKind
from footprint.decision.stop import StopKeeper
from footprint.execution.executor.diego import Executor, Position
from footprint.execution.halt import Halt
from footprint.execution.journal.josephine import Journal
from footprint.execution.ledger import Ledger, SoldHold, StateStore
from footprint.execution.management.malik import Alarm, Manager
from footprint.execution.orders import Quote, SimulatedBroker, TrackedBroker
from footprint.execution.runner import Runner
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import SEND, Stack
from tests.execution.test_fill_price_pending import (
    Lag,
    _check_the_fill,
    _exit_filled_unpriced,
    _reconcile_rounds,
    _wait,
)

D = Decimal
SEC = timedelta(seconds=1)


def _restart(stack: Stack, tmp_path: Path) -> tuple[Runner, Ledger, Halt, list[Alarm]]:
    """A new process on the same broker and the same ledger file: every in-memory
    record (the manager's pending sells, the halt, the stops) is gone."""
    ledger = Ledger(stack.profiles.risk, stack.profiles.instrument, StateStore(stack.state_path))
    halt = Halt()
    tracked = TrackedBroker(stack.broker)
    stops = StopKeeper(stack.profiles.risk, stack.profiles.instrument, stack.profiles.execution)
    executor = Executor(stack.profiles, stack.pipeline, tracked, stops, ledger, halt, stack.book)
    manager = Manager(stack.profiles, tracked, stops, ledger, halt, stack.book)
    alarms: list[Alarm] = []
    runner = Runner(
        stack.pipeline,
        executor,
        manager,
        ledger,
        Journal(tmp_path / "journal-restart"),
        halt,
        bars=lambda now: [],
        chain=lambda now: None,
        blackouts=lambda now: (),
        on_alarm=alarms.append,
        started=DECISION_TIME,
        book=stack.book,
        settings=stack.profiles.execution.runner,
    )
    return runner, ledger, halt, alarms


def _run(stack: Stack, runner: Runner, start: datetime, seconds: int) -> datetime:
    t = start
    for second in range(seconds):
        t = start + second * SEC
        stack.quote(150, 151, t)
        runner.step(t)
    return t


def _reconcile_sells(lag: Lag) -> list[str]:
    return [o for o in lag.placed if o.startswith("reconcile-")]


def _many(stack: Stack) -> int:
    """Steps enough for several reconcile rounds through both settle waits."""
    return 8 * (stack.profiles.execution.reconcile.settle_seconds + 1)


def test_a_restart_past_the_wait_never_sells_beside_the_filled_sell(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reviewer's probe, at the runner level: the D40 hold is reached, the bot is
    restarted, and the broker still reports the contract held. No reconcile sell."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    _reconcile_rounds(stack, position, t + _wait(stack), 3)
    assert len(_check_the_fill(stack)) == 1
    runner, ledger, halt, alarms = _restart(stack, tmp_path)
    late = t + 3 * _wait(stack)
    _run(stack, runner, late, _many(stack))
    assert _reconcile_sells(lag) == []
    assert lag.sells() == [f"{position.position_id}-exit-1"]  # the one real sale only
    assert SimulatedBroker.positions(stack.broker) == {}  # never short at the broker
    assert runner.reconciling
    assert halt.tripped
    assert [r for r in halt.reasons if "filled before a restart" in r]  # names the hold
    assert ledger.account(late).kill_switch_latched
    restarted = [a.message for a in alarms if "CHECK THE FILL" in a.message]
    assert len(restarted) == 1  # once, not every round
    assert f"{position.position_id}-exit-1" in restarted[0]
    assert "flatten by hand" in restarted[0]
    assert "book_by_hand" in restarted[0]


def test_a_restart_inside_the_wait_never_sells_beside_the_filled_sell(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hold is durable from the moment the filled sell is known, not only once the
    wait has run out: a restart inside the wait loses nothing either."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    assert stack.ledger.sold_unpriced == (
        SoldHold(position.position_id, position.contract_id, f"{position.position_id}-exit-1"),
    )
    assert not _check_the_fill(stack)
    runner, ledger, _, alarms = _restart(stack, tmp_path)
    _run(stack, runner, t + SEC, _many(stack))
    assert _reconcile_sells(lag) == []
    assert SimulatedBroker.positions(stack.broker) == {}
    assert ledger.account(t + SEC).kill_switch_latched
    assert len([a for a in alarms if "CHECK THE FILL" in a.message]) == 1


def test_a_hold_found_by_reconcile_itself_is_durable(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile, not management, finds the filled sell: it is kept durably too."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    fresh = Manager(
        stack.profiles,
        stack.manager._broker,
        stack.stops,
        stack.ledger,
        stack.halt,
        stack.book,
    )
    fresh._exit_attempts[position.position_id] = 1  # it knows the exit it sent
    stack.ledger._save(replace(stack.ledger._state, sold_unpriced=()))  # as if never saved
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    for k in range(3):
        stack.quote(150, 151, t + k * (settle + SEC))
        fresh.reconcile(position, t + k * (settle + SEC))
    assert [h.order_id for h in stack.ledger.sold_unpriced] == [f"{position.position_id}-exit-1"]
    assert _reconcile_sells(lag) == []


def test_the_hold_is_released_when_the_sale_is_booked_at_its_real_price(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    assert stack.ledger.sold_unpriced
    lag.reveal()
    closed = stack.manager.flatten(position, t + 5 * SEC)
    assert closed is not None
    assert closed.exit_price == D(160)
    assert stack.ledger.sold_unpriced == ()
    assert stack.ledger.open_position_id is None


def test_the_hold_is_released_when_reconcile_books_the_real_price(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    late = t + _wait(stack)
    _reconcile_rounds(stack, position, late, 3)
    lag.reveal()
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    closes = []
    for k in range(3, 8):
        now = late + k * (settle + SEC)
        stack.quote(150, 151, now)
        stack.manager.reconcile(position, now)
        closes += stack.manager.take_reconcile_closes()
    assert [c.exit_price for c in closes] == [D(160)]
    assert stack.ledger.sold_unpriced == ()


def test_booking_the_position_by_hand_releases_its_hold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Andre checked the fill, flattened by hand and booked it from the statement:
    the hold goes with the position, and only that position's hold."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    other = SoldHold("other-position", "OTHER", "other-exit-1")
    stack.ledger.hold_sold(other)
    _, ledger, _, _ = _restart(stack, tmp_path)
    ledger.book_by_hand(t + SEC, position.position_id, D(-100), "Andre")
    assert ledger.sold_unpriced == (other,)
    assert ledger.open_position_id is None


def test_a_position_whose_filled_sell_this_process_cannot_find_is_still_held(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The position is known, but this manager has no record of the sell that sold it
    (the exit was sent by an earlier process, so it is not among its candidates): the
    saved hold still stops every reconcile sell."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    fresh = Manager(
        stack.profiles, stack.manager._broker, stack.stops, stack.ledger, stack.halt, stack.book
    )
    alarms: list[Alarm] = []
    fresh.route_alarms(alarms.append)
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    for k in range(6):
        now = t + k * (settle + SEC)
        stack.quote(150, 151, now)
        assert not fresh.reconcile(position, now)
    assert _reconcile_sells(lag) == []
    assert SimulatedBroker.positions(stack.broker) == {}
    assert len([a for a in alarms if "CHECK THE FILL" in a.message]) == 1


def test_booking_a_never_recorded_position_by_hand_releases_only_its_hold(
    stack: Stack,
) -> None:
    """A hold whose position the ledger never recorded as open (its write failed at
    the fill) goes when Andre books that position by hand; the open position's
    holds stay."""
    open_hold = SoldHold("open-position", "C-1", "open-exit-1")
    lost_hold = SoldHold("lost-position", "C-2", "lost-exit-1")
    stack.ledger.on_entry_filled(SEND, "open-position", SessionKind.PRIME)
    stack.ledger.hold_sold(open_hold)
    stack.ledger.hold_sold(lost_hold)
    stack.ledger.book_by_hand(SEND + SEC, "lost-position", D(-50), "Andre")
    assert stack.ledger.sold_unpriced == (open_hold,)
    assert stack.ledger.open_position_id == "open-position"


def test_clearing_the_kill_switch_does_not_release_a_hold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    _reconcile_rounds(stack, position, t + _wait(stack), 2)
    stack.ledger.clear_kill_switch("Andre")
    assert [h.position_id for h in stack.ledger.sold_unpriced] == [position.position_id]


def test_a_hold_that_cannot_be_saved_stands_down_and_says_so(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: the in-memory hold still stops every sell, the system halts, and
    Andre is told not to restart while CHECK THE FILL is unsaved."""
    lag = Lag(stack.broker, monkeypatch)

    def broken(hold: SoldHold) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stack.ledger, "hold_sold", broken)
    position, t = _exit_filled_unpriced(stack, lag)
    assert stack.halt.tripped
    unsaved = [a.message for a in stack.alarms if "could not save" in a.message]
    assert unsaved
    assert "do not restart" in unsaved[0]
    for k in range(1, 4):
        stack.quote(150, 151, t + k * SEC)
        assert stack.manager.flatten(position, t + k * SEC) is None
    assert lag.sells() == [f"{position.position_id}-exit-1"]


def test_the_state_file_round_trips_holds_and_reads_an_old_file(tmp_path: Path) -> None:
    import json

    from footprint.core.config import load_profiles
    from tests.conftest import REPO_ROOT

    profiles = load_profiles(REPO_ROOT / "config", "nq.yaml")
    store = StateStore(tmp_path / "ledger.json")
    ledger = Ledger(profiles.risk, profiles.instrument, store, create=True)
    hold = SoldHold("p-1", "C-1", "p-1-exit-1")
    ledger.hold_sold(hold)
    ledger.hold_sold(hold)  # idempotent
    reread = Ledger(profiles.risk, profiles.instrument, store)
    assert reread.sold_unpriced == (hold,)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    del raw["sold_unpriced"]  # a state file written before this field existed
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    assert Ledger(profiles.risk, profiles.instrument, store).sold_unpriced == ()


# --- G2-2: reviewer mutants R-b and R-c ----------------------------------------------


def test_a_latch_that_fails_on_the_first_overdue_round_is_retried(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-b: the durable latch save fails on the first overdue round; a later round
    must still latch it (the alarm itself is sent once)."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    real = stack.ledger.latch_kill_switch
    failures = {"left": 1}

    def flaky(reason: str) -> None:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise OSError("disk full")
        real(reason)

    monkeypatch.setattr(stack.ledger, "latch_kill_switch", flaky)
    _reconcile_rounds(stack, position, t + _wait(stack), 6)
    assert failures["left"] == 0
    assert stack.ledger.account(t + 2 * _wait(stack)).kill_switch_latched
    assert len(_check_the_fill(stack)) == 1


def test_a_stray_holding_beside_a_hold_is_still_flattened(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-c: the hold covers its own contract only. Another contract held beside it is
    flattened as before."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    stray = _stray_contract(stack, position)
    assert lag.held is not None
    lag.hold({**lag.held, stray: 1})
    late = t + _wait(stack)
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    for k in range(6):
        now = late + k * (settle + SEC)
        stack.quote(150, 151, now)
        stack.book.publish(Quote(stray, D(9), D(10), now))
        stack.manager.reconcile(position, now)
    sells = _reconcile_sells(lag)
    assert sells
    assert all(o.endswith(f"-{stray}") for o in sells)  # never the held contract


def _stray_contract(stack: Stack, position: Position) -> str:
    return f"{position.contract_id}-STRAY"
