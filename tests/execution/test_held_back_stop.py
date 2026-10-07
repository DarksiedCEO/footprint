"""AEGIS I-1: a held position whose resting stop goes Inactive at IBKR, and whose cancel
IBKR never answers. Through the runner, over the fake IBKR: the system must never go on
"managing" an unprotected position as if protected: reconcile disagrees and halts, then
gets out (here by re-pricing the stuck stop) or escalates the stuck cancel to Andre
(CANCEL STUCK), and never sells beside the stuck stop."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker, IbkrSettings
from footprint.core.models import Bar
from footprint.execution.orders import OrderState, TrackedBroker
from tests.brokers.fake_ibkr import FakeIbkr
from tests.brokers.test_ibkr_adapter import FAST, contract
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

D = Decimal
SEC = timedelta(seconds=1)


def through_ibkr(stack: Stack) -> tuple[FakeIbkr, IbkrBroker]:
    """The stack's executor and manager over the real adapter and a fake IBKR."""
    contracts = ContractBook()
    contracts.register(stack.contract, contract(501))
    fake = FakeIbkr(stack.book, contracts)
    adapter = IbkrBroker(fake, contracts, IbkrSettings(FAST, FAST, FAST * 3), ("NQ", "FOP"))
    fake.attach(adapter)
    fake.connect(next_valid_id=1)
    tracked = TrackedBroker(adapter)
    stack.executor._broker = tracked
    stack.manager._broker = tracked
    return fake, adapter


def test_a_held_position_with_an_unanswered_inactive_stop_escalates_to_andre(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter = through_ibkr(stack)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    t = DECISION_TIME
    for k in range(120):  # the entry, then its fill
        if runner.position is not None:
            break
        stack.quote(150, D(152) if k < 3 else D(151), t)
        runner.step(t)
        t += SEC
    position = runner.position
    assert position is not None
    stop = next(c for c in fake.copies.values() if c.request.order_ref == position.stop_order_id)
    fake.mute_cancels = True  # IBKR never answers a cancel
    fake.inactivate(stop.perm_id)
    before, sent_before = len(alarms), len(fake.sends)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(6 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    held = fake.sim.positions()
    assert any("reconcile mismatch" in m for m in said), said  # never silently "agreed"
    assert stack.halt.tripped
    # The stuck stop is re-priced toward the market (a modify to IBKR), never doubled.
    assert any(r.order_ref == position.stop_order_id for r in fake.sends[sent_before:])
    # Out, or Andre told the stop is stuck: never managed on as if protected.
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())  # never sold beside the stuck stop
    sells = [
        c
        for c in fake.copies.values()
        if c.request.action == "SELL" and fake.sim.status(c.sim_id).state is OrderState.WORKING
    ]
    assert sum(c.request.quantity for c in sells) <= sum(held.values())  # never more than held


def test_a_working_stop_whose_cancel_stays_pending_escalates_to_andre(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS J-1: an ordinary working stop; an exit cancels it and IBKR leaves the
    cancel pending forever. A stop being cancelled is no protection: reconcile must
    disagree and escalate (CANCEL STUCK), never loop or carry the position on."""
    fake, adapter = through_ibkr(stack)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    t = DECISION_TIME
    for k in range(120):
        if runner.position is not None:
            break
        stack.quote(150, D(152) if k < 3 else D(151), t)
        runner.step(t)
        t += SEC
    position = runner.position
    assert position is not None
    fake.slow_cancels = True  # every cancel stays PendingCancel: IBKR never finishes it
    fake.cancel_answer = None
    stack.ledger.latch_kill_switch("test: force an exit")  # management must get out
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(8 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    held = fake.sim.positions()
    assert any("reconcile mismatch" in m for m in said), said
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())


def _held_position(stack: Stack, bars: list[Bar], tmp_path: Path):  # type: ignore[no-untyped-def]
    fake, adapter = through_ibkr(stack)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    t = DECISION_TIME
    for k in range(120):
        if runner.position is not None:
            break
        stack.quote(150, D(152) if k < 3 else D(151), t)
        runner.step(t)
        t += SEC
    assert runner.position is not None
    return fake, adapter, runner, alarms, t


def test_a_normal_exit_through_a_brief_pending_cancel_and_a_doubt_ends_cleanly(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS K-1/K-3: an ordinary exit whose stop cancel sits in PendingCancel for a few
    seconds (under cancel_escalate_seconds), with a doubt meanwhile, is a healthy exit:
    no reconcile mismatch, no "held back" alarm, closed with its own reason."""
    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    fake.slow_cancels = True
    stack.ledger.latch_kill_switch("test: force an exit")
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    pending = escalate // 2
    for k in range(pending):
        if k == 1:
            runner.doubt()  # reconcile runs while the cancel is pending
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    assert not stack.halt.tripped
    fake.slow_cancels = False
    fake.finish_cancels()
    for _ in range(2 * escalate):
        if runner.position is None and not runner.reconciling:
            break
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    assert not any("reconcile mismatch" in m for m in said), said
    assert not any("held back" in m for m in said), said
    assert not any("not by this system" in m for m in said), said
    assert runner.position is None
    assert [c.reason.value for c in runner.closed][-1:] == ["KILL_SWITCH"], runner.closed
    assert fake.sim.positions() == {}


def test_a_stop_cancelled_by_someone_else_is_no_protection(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """A stop whose cancel this system never sent (by hand at IBKR), left pending: an
    alarm, then the exit; never managed on as if protected, never a sell beside it."""
    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    assert position.stop_order_id is not None
    fake.slow_cancels = True
    adapter.cancel(position.stop_order_id)  # not through management
    assert adapter.status(position.stop_order_id).cancelling
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(6 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    assert sum("not by this system" in m for m in said) == 1, said
    held = fake.sim.positions()
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())


def test_reconcile_agrees_with_a_pending_cancel_only_until_the_escalation(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS K-1: reconcile agrees with a stop whose cancel is pending for under
    cancel_escalate_seconds (a normal exit passes through it), and no longer after."""
    fake, adapter, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    assert position.stop_order_id is not None
    fake.slow_cancels = True
    adapter.cancel(position.stop_order_id)
    escalate = timedelta(seconds=stack.profiles.execution.reconcile.cancel_escalate_seconds)
    manager = stack.manager
    assert manager.reconcile(position, t)
    assert manager.reconcile(position, t + escalate - SEC)
    assert not manager.reconcile(position, t + escalate)


# --- AEGIS L2 (audit of 9ca890a) -------------------------------------------------------


def _steps(stack: Stack, runner, t, n: int):  # type: ignore[no-untyped-def]
    for _ in range(n):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    return t


def _force_reason(stack: Stack, reason, steps: int) -> None:  # type: ignore[no-untyped-def]
    """Management's exit check answers `reason` for the next `steps` calls."""
    original = stack.manager._must_exit
    left = {"n": steps}

    def forced(*args, **kwargs):  # type: ignore[no-untyped-def]
        if left["n"] > 0:
            left["n"] -= 1
            return reason
        return original(*args, **kwargs)

    stack.manager._must_exit = forced  # type: ignore[method-assign]


def _stop_copy(fake: FakeIbkr, order_id: str):  # type: ignore[no-untyped-def]
    return next(c for c in fake.copies.values() if c.request.order_ref == order_id)


def test_a_doubt_as_our_exit_cancel_lands_never_latches_the_day(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS L2-1: a NEWS exit (which does not latch by itself); the stop's cancel is
    PendingCancel, then lands in the same step a doubt is raised (as on a reconnect).
    Reconcile must take the stop as expected gone: no mismatch, no latch, NEWS."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    before = len(alarms)
    t = _steps(stack, runner, t, 3)
    fake.slow_cancels = False
    fake.finish_cancels()
    runner.doubt()
    t = _steps(stack, runner, t, 30)
    said = [a.message for a in alarms[before:]]
    assert not any("reconcile mismatch" in m for m in said), said
    assert not any("not cancelled" in m for m in said), said  # AEGIS L2-4: no false alarm
    assert not stack.ledger.account(t).kill_switch_latched
    assert not stack.halt.tripped
    assert [c.reason for c in runner.closed] == [ExitReason.NEWS]
    assert fake.sim.positions() == {}
    # The acknowledged cancel was sent once, never again each step (nor after it landed).
    assert fake.cancels.count(_stop_copy(fake, stop_id).ib_id) == 1


def test_a_doubt_while_positions_settle_and_the_cancel_lands_never_latches(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS L2-1 (K1c): reconcile cannot agree at first (positions settling); our
    exit's cancel lands meanwhile: still no mismatch."""
    from footprint.execution.orders import PositionsUncertain

    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    fake.slow_cancels = True
    stack.ledger.latch_kill_switch("test: force an exit")
    before = len(alarms)
    t = _steps(stack, runner, t, 2)
    original = stack.manager._broker.positions
    calls = {"n": 0}

    def settling() -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PositionsUncertain("positions settling")
        return original()

    stack.manager._broker.positions = settling  # type: ignore[method-assign]
    runner.doubt()
    t = _steps(stack, runner, t, 1)
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 30)
    said = [a.message for a in alarms[before:]]
    assert not any("reconcile mismatch" in m for m in said), said
    assert fake.sim.positions() == {}


def test_a_refused_exit_cancel_then_a_hand_cancel_still_alarms(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS L2-2: an exit's stop cancel goes PendingCancel and is refused (Submitted
    again); the exit reason passes, management resumes. A later cancel by hand is not
    ours: it alarms as such and is booked UNPROTECTED, never as the old exit."""
    from footprint.execution.management.malik import ExitReason

    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.STOP, 1)
    t = _steps(stack, runner, t, 2)
    fake.pending_cancels.clear()
    fake.slow_cancels = False
    fake._report(force=child.ib_id)  # IBKR refused the cancel: Submitted again
    t = _steps(stack, runner, t, 3)
    assert adapter.status(stop_id).protects
    assert runner.position is not None
    before = len(alarms)
    fake.slow_cancels = True
    adapter.cancel(stop_id)  # by hand
    t = _steps(stack, runner, t, 3)
    said = [a.message for a in alarms[before:]]
    assert sum("not by this system" in m for m in said) == 1, said
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 20)
    assert [c.reason for c in runner.closed] == [ExitReason.UNPROTECTED], runner.closed
    assert all(qty >= 0 for qty in fake.sim.positions().values())


def test_a_refused_cancel_leaves_no_stale_clock_for_a_later_one(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS L2-3: management saw a PendingCancel that IBKR then refused; a minute later
    a fresh, brief PendingCancel gets the full cancel_escalate_seconds in reconcile."""
    fake, adapter, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    adapter.cancel(stop_id)
    stack.manager.manage(position, (), t)  # management sees the PendingCancel
    fake.pending_cancels.clear()
    fake._report(force=child.ib_id)  # refused: Submitted again
    t += SEC
    stack.quote(150, D(151), t)
    stack.manager.manage(position, (), t)  # management sees it acting again
    t += 60 * SEC
    adapter.cancel(stop_id)  # a fresh, brief PendingCancel
    assert stack.manager.reconcile(position, t)


def test_reconcile_clears_its_pending_cancel_clock_when_the_cancel_is_refused(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Reconcile's own clock: refused (Submitted again) between two reconciles, a later
    PendingCancel starts a new window."""
    fake, adapter, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    adapter.cancel(stop_id)
    assert stack.manager.reconcile(position, t)
    fake.pending_cancels.clear()
    fake._report(force=child.ib_id)  # refused
    assert stack.manager.reconcile(position, t + SEC)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    later = t + (escalate + 5) * SEC
    adapter.cancel(stop_id)
    assert stack.manager.reconcile(position, later)


def test_a_hand_cancel_seen_by_management_counts_from_then_in_reconcile(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The PendingCancel clock starts when management first sees it: reconcile run
    later disagrees once cancel_escalate_seconds have passed since then."""
    fake, adapter, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    fake.slow_cancels = True
    adapter.cancel(stop_id)  # by hand
    stack.manager.manage(position, (), t)
    escalate = timedelta(seconds=stack.profiles.execution.reconcile.cancel_escalate_seconds)
    assert not stack.manager.reconcile(position, t + escalate)


def test_an_exit_keeps_the_reason_it_began_with(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """A NEWS exit whose stop cancel is pending; the failsafe flatten runs meanwhile;
    when the cancel lands the exit goes out, booked as the reason it began with."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    fake.slow_cancels = True
    manager = stack.manager
    _force_reason(stack, ExitReason.NEWS, 1)
    assert manager.manage(position, (), t) is None  # NEWS exit: cancel pending
    t += SEC
    assert manager.flatten(position, t, ExitReason.FAILSAFE) is None  # still pending
    fake.slow_cancels = False
    fake.finish_cancels()
    closed = None
    for _ in range(20):
        t += SEC
        stack.quote(150, D(151), t)
        closed = manager.manage(position, (), t)
        if closed is not None:
            break
    assert closed is not None
    assert closed.reason is ExitReason.NEWS


def test_a_stop_cancelled_by_reconcile_is_not_reported_as_someone_elses(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Reconcile cancelled the stop (a disagreement), then agreed while its cancel was
    pending: management gets out without calling it a cancel by someone else."""
    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    fake.slow_cancels = True
    manager = stack.manager
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    assert not manager.reconcile(None, t)  # disagrees (a position is held)...
    assert not manager.reconcile(None, t + settle)  # ...and acts: cancels the stop
    t += settle + SEC
    assert manager.reconcile(position, t)  # agrees: the stop's cancel is pending
    before = len(alarms)
    manager.manage(position, (), t + SEC)
    said = [a.message for a in alarms[before:]]
    assert not any("not by this system" in m for m in said), said


def test_a_hand_cancel_alarms_once_even_while_the_exit_cannot_start(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The exit waits (positions uncertain, no cancel sent yet): the hand-cancel alarm
    is still raised once, not every step."""
    from footprint.execution.orders import PositionsUncertain

    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    fake.slow_cancels = True
    adapter.cancel(stop_id)

    def uncertain() -> dict[str, int]:
        raise PositionsUncertain("settling")

    stack.manager._broker.positions = uncertain  # type: ignore[method-assign]
    before = len(alarms)
    for _ in range(4):
        stack.manager.manage(position, (), t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    assert sum("not by this system" in m for m in said) == 1, said


def test_a_refused_reconcile_cancel_then_a_hand_cancel_still_alarms(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Reconcile cancelled the stop, IBKR refused (Submitted again), reconcile agreed,
    Andre cleared the kill switch and management resumed: a later cancel by hand is
    someone else's, and alarms as such."""
    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    manager = stack.manager
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    assert not manager.reconcile(None, t)
    assert not manager.reconcile(None, t + settle)  # acts: cancels the stop
    fake.pending_cancels.clear()
    fake._report(force=child.ib_id)  # refused: Submitted again
    t += settle + SEC
    assert manager.reconcile(position, t)
    stack.ledger.clear_kill_switch("Andre")  # (reconcile latched it; Andre clears it)
    t += SEC
    stack.quote(150, D(151), t)
    assert manager.manage(position, (), t) is None  # managed normally
    before = len(alarms)
    adapter.cancel(stop_id)  # by hand
    manager.manage(position, (), t + SEC)
    said = [a.message for a in alarms[before:]]
    assert any("not by this system" in m for m in said), said


def test_an_abandoned_exit_leaves_no_stuck_clock_for_a_later_exit(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """An exit's stop cancel is pending, then refused; the exit is abandoned. A minute
    later a new exit's cancel gets the full cancel_escalate_seconds, not none."""
    from footprint.execution.management.malik import ExitReason, NeedsReconcile

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    child = _stop_copy(fake, stop_id)
    manager = stack.manager
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    for _ in range(3):  # the exit waits on the pending cancel
        stack.quote(150, D(151), t)
        assert manager.manage(position, (), t) is None
        t += SEC
    fake.pending_cancels.clear()
    fake._report(force=child.ib_id)  # refused: Submitted again
    stack.quote(150, D(151), t)
    assert manager.manage(position, (), t) is None  # managed normally: abandoned
    t += 60 * SEC
    _force_reason(stack, ExitReason.NEWS, 1)
    stack.quote(150, D(151), t)
    try:
        manager.manage(position, (), t)
    except NeedsReconcile as exc:  # a stale clock would escalate at once
        raise AssertionError(f"escalated at once: {exc}") from exc
    assert not stack.halt.tripped


# --- AEGIS M3 (audit of c2a8935) -------------------------------------------------------


def _quiet_steps(stack: Stack, runner, t, n: int):  # type: ignore[no-untyped-def]
    """Steps with no new quote (the last one goes stale)."""
    for _ in range(n):
        runner.step(t)
        t += SEC
    return t


def _first_positions_uncertain(stack: Stack) -> None:
    from footprint.execution.orders import PositionsUncertain

    original = stack.manager._broker.positions
    calls = {"n": 0}

    def uncertain() -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PositionsUncertain("settling")
        return original()

    stack.manager._broker.positions = uncertain  # type: ignore[method-assign]


def test_a_refused_cancel_then_a_stale_quote_then_a_hand_cancel_still_alarms(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS M3-1: the refusal itself ends the exit (quote or no quote): a later hand
    cancel during a stale quote is reported as someone else's."""
    from footprint.execution.management.malik import ExitReason

    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.STOP, 1)
    t = _steps(stack, runner, t, 2)
    fake.pending_cancels.clear()
    fake.slow_cancels = False
    fake._report(force=child.ib_id)  # refused: Submitted again
    t += 10 * SEC  # the last quote is now stale
    t = _quiet_steps(stack, runner, t, 2)
    assert any("refused a cancel" in a.message for a in alarms)
    before = len(alarms)
    fake.slow_cancels = True
    adapter.cancel(stop_id)  # by hand
    t = _quiet_steps(stack, runner, t, 2)
    said = [a.message for a in alarms[before:]]
    assert sum("not by this system" in m for m in said) == 1, said
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 30)
    said = [a.message for a in alarms[before:]]
    assert not any("CANCELLED at the broker" in m for m in said), said  # alarmed once only
    # The exit never stopped wanting out (the stale quote never cleared it), so its
    # first cancel's clock hands it to reconcile (AEGIS N4-1): out either way.
    assert [c.reason for c in runner.closed] in (
        [ExitReason.UNPROTECTED],
        [ExitReason.RECONCILE],
    ), runner.closed
    assert fake.sim.positions() == {}


def test_an_exit_wanting_out_through_a_refusal_and_a_stale_gap_escalates(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS N4-1 (MG revisited): the exit's reason is never seen to pass (a refused
    cancel, then stale quotes, then the reason still holds): the exit has wanted the
    stop gone since its first cancel, so it is handed to reconcile at once; a refusal
    never restarts the escalation window."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 2)
    fake.pending_cancels.clear()
    fake.slow_cancels = False
    fake._report(force=child.ib_id)  # refused
    t += 20 * SEC
    t = _quiet_steps(stack, runner, t, 2)
    before = len(alarms)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 5)
    t = _steps(stack, runner, t, 1)
    said = [a.message for a in alarms[before:]]
    assert any("will not cancel" in m for m in said), said
    assert stack.halt.tripped


def test_a_hand_cancel_after_an_exit_that_sent_nothing_is_booked_unprotected(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS M3-2: an exit that could not send anything (positions uncertain) records
    no reason; the hand-cancelled stop is booked UNPROTECTED, and alarmed."""
    from footprint.execution.management.malik import ExitReason

    _, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    _first_positions_uncertain(stack)
    _force_reason(stack, ExitReason.NEWS, 1)
    before = len(alarms)
    t = _steps(stack, runner, t, 1)
    adapter.cancel(stop_id)  # by hand, lands at once
    t = _steps(stack, runner, t, 30)
    said = [a.message for a in alarms[before:]]
    assert any("CANCELLED at the broker" in m for m in said), said
    assert [c.reason for c in runner.closed] == [ExitReason.UNPROTECTED], runner.closed


def test_a_failsafe_flatten_after_an_exit_that_sent_nothing_is_booked_failsafe(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS M3-2 (MF2): management breaks after an exit that sent nothing; the
    runner's failsafe flattens and the trade is booked FAILSAFE."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    _first_positions_uncertain(stack)
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 1)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("management broken")

    stack.manager.manage = broken  # type: ignore[method-assign]
    t = _steps(stack, runner, t, 80)
    assert fake.sim.positions() == {}
    assert [c.reason for c in runner.closed] == [ExitReason.FAILSAFE], runner.closed


def test_an_exit_is_booked_as_the_reason_that_sent_its_cancel(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS M3-2: a first exit (NEWS) sends nothing (positions uncertain); the next
    step's exit (CLOSE) sends the stop's cancel; it is booked CLOSE, not NEWS."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    _first_positions_uncertain(stack)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 1)  # NEWS: nothing sent
    _force_reason(stack, ExitReason.CLOSE, 1)
    t = _steps(stack, runner, t, 2)  # CLOSE: the cancel goes out, pending
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 20)
    assert [c.reason for c in runner.closed] == [ExitReason.CLOSE], runner.closed


def test_a_cancel_landing_mid_step_keeps_the_exit_reason(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The stop is read PendingCancel, then the cancel lands before the exit's own
    read in the same step (the real adapter is threaded): the exit that goes out is
    booked with the reason it began with."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 2)  # NEWS exit; cancel pending
    broker = stack.manager._broker
    plain = broker.status
    reads = {"n": 0}

    def lands_on_second_read(order_id: str):  # type: ignore[no-untyped-def]
        reads["n"] += 1
        if reads["n"] == 2:
            fake.slow_cancels = False
            fake.finish_cancels()
        return plain(order_id)

    broker.status = lands_on_second_read  # type: ignore[method-assign]
    stack.quote(150, D(151), t)
    closed = stack.manager.manage(runner.position, (), t)
    broker.status = plain  # type: ignore[method-assign]
    for _ in range(20):
        if closed is not None:
            break
        t += SEC
        stack.quote(150, D(151), t)
        closed = stack.manager.manage(runner.position, (), t)
    assert closed is not None
    assert closed.reason is ExitReason.NEWS


def test_repeated_cancel_refusals_still_escalate_in_time(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS N4-1: IBKR refuses every cancel while the exit reason holds: handed to
    reconcile within cancel_escalate_seconds of the first cancel, the refusal alarmed
    once (no flood), and out in the end, never short."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    child = _stop_copy(fake, stop_id)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.CLOSE, 10 * escalate)
    before = len(alarms)
    escalated_at = None
    for k in range(3 * escalate):
        t = _steps(stack, runner, t, 1)
        if child.perm_id in fake.pending_cancels:  # IBKR refuses the cancel
            fake.pending_cancels.remove(child.perm_id)
            fake._report(force=child.ib_id)
        if escalated_at is None and stack.halt.tripped:
            escalated_at = k
    said = [a.message for a in alarms[before:]]
    assert escalated_at is not None, said
    assert escalated_at <= escalate + 1, escalated_at
    assert sum("refused a cancel" in m for m in said) == 1, said
    fake.slow_cancels = False
    t = _steps(stack, runner, t, 6 * escalate)
    assert all(qty >= 0 for qty in fake.sim.positions().values())


# --- AEGIS N4 (audit of 97081a6) -------------------------------------------------------


def test_a_stale_submitted_between_pending_cancels_keeps_our_exit_ours(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS N4-2: PendingCancel, a stale Submitted, PendingCancel again: the stop is
    being cancelled when management looks, so the cancel stays ours: no 'not by this
    system' alarm, and the exit is booked with its own reason."""
    from footprint.execution.management.malik import ExitReason

    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    child = _stop_copy(fake, stop_id)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 1)
    before = len(alarms)
    adapter.on_order_status(child.ib_id, child.perm_id, "Submitted", 0, 1, D(0))  # stale
    adapter.on_order_status(child.ib_id, child.perm_id, "PendingCancel", 0, 1, D(0))
    t = _steps(stack, runner, t, 2)
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 30)
    said = [a.message for a in alarms[before:]]
    assert not any("not by this system" in m for m in said), said
    assert [c.reason for c in runner.closed] == [ExitReason.NEWS], runner.closed


def test_a_refusal_then_a_new_pending_cancel_starts_a_new_window(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """A PendingCancel seen at t0, refused and cancelled again before the next step at
    t1: reconcile counts the new cancel from t1, not t0."""
    fake, adapter, runner, _, t0 = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    child = _stop_copy(fake, stop_id)
    manager = stack.manager
    fake.slow_cancels = True
    adapter.cancel(stop_id)  # by hand
    stack.quote(150, D(151), t0)
    manager.manage(position, (), t0)  # seen PendingCancel at t0 (we cancel it too)
    fake.pending_cancels.clear()
    fake._report(force=child.ib_id)  # refused: Submitted
    adapter.cancel(stop_id)  # cancelled again: PendingCancel
    escalate = timedelta(seconds=stack.profiles.execution.reconcile.cancel_escalate_seconds)
    t1 = t0 + escalate / 2
    stack.quote(150, D(151), t1)
    manager.manage(position, (), t1)
    assert manager._cancelling_since.get(stop_id) == t1


def test_an_exit_keeps_its_first_reason_across_a_resent_cancel(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """A NEWS exit's cancel goes unanswered; next step a CLOSE exit sends it again
    (acknowledged this time); when it lands the exit is booked NEWS, its first."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    fake.mute_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 1)  # NEWS: cancel sent, never answered
    fake.mute_cancels = False
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.CLOSE, 1)
    t = _steps(stack, runner, t, 1)  # CLOSE: sent again, PendingCancel
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 20)
    assert [c.reason for c in runner.closed] == [ExitReason.NEWS], runner.closed


def test_a_failsafe_flatten_after_our_cancel_landed_is_booked_failsafe(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The exit sent its cancel (NEWS) and it landed; the failsafe flatten then places
    the exit: booked FAILSAFE, the reason it was passed."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    manager = stack.manager
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    stack.quote(150, D(151), t)
    assert manager.manage(position, (), t) is None
    fake.slow_cancels = False
    fake.finish_cancels()
    closed = None
    for _ in range(20):
        t += SEC
        stack.quote(150, D(151), t)
        closed = manager.flatten(position, t, ExitReason.FAILSAFE)
        if closed is not None:
            break
    assert closed is not None
    assert closed.reason is ExitReason.FAILSAFE


def _dropped_exit(stack: Stack, runner, fake: FakeIbkr, t):  # type: ignore[no-untyped-def]
    """A NEWS exit sends the stop's cancel, IBKR never answers it, and the reason
    passes next step: the exit is dropped (the stop still protects)."""
    from footprint.execution.management.malik import ExitReason

    fake.mute_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    t = _steps(stack, runner, t, 2)
    fake.mute_cancels = False
    return t


def test_a_hand_cancel_after_a_dropped_exit_is_someone_elses(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    stop_id = runner.position.stop_order_id
    t = _dropped_exit(stack, runner, fake, t)
    before = len(alarms)
    fake.slow_cancels = True
    adapter.cancel(stop_id)  # by hand
    t = _steps(stack, runner, t, 1)
    said = [a.message for a in alarms[before:]]
    assert any("not by this system" in m for m in said), said


def test_a_new_exit_after_a_dropped_exit_is_booked_as_itself(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    t = _dropped_exit(stack, runner, fake, t)
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.CLOSE, 1)
    t = _steps(stack, runner, t, 1)
    fake.slow_cancels = False
    fake.finish_cancels()
    t = _steps(stack, runner, t, 20)
    assert [c.reason for c in runner.closed] == [ExitReason.CLOSE], runner.closed


def test_a_hand_cancel_after_an_unanswered_reconcile_cancel_is_someone_elses(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Reconcile's cancel of the stop is never answered; reconcile agrees, Andre clears
    the kill switch and management resumes: a later hand cancel is someone else's."""
    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    stop_id = position.stop_order_id
    assert stop_id is not None
    manager = stack.manager
    fake.mute_cancels = True
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    assert not manager.reconcile(None, t)
    assert not manager.reconcile(None, t + settle)  # acts: cancels the stop (no answer)
    fake.mute_cancels = False
    t += settle + SEC
    assert manager.reconcile(position, t)
    stack.ledger.clear_kill_switch("Andre")
    t += SEC
    stack.quote(150, D(151), t)
    assert manager.manage(position, (), t) is None  # managed normally
    before = len(alarms)
    fake.slow_cancels = True
    adapter.cancel(stop_id)  # by hand
    manager.manage(position, (), t + SEC)
    said = [a.message for a in alarms[before:]]
    assert any("not by this system" in m for m in said), said


def test_a_resting_failsafe_exit_after_our_cancel_landed_is_booked_failsafe(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """As above, but the failsafe's exit rests (no bid) and fills later: still booked
    FAILSAFE, the reason it was placed with, not the exit's first reason."""
    from footprint.execution.management.malik import ExitReason

    fake, _, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    manager = stack.manager
    fake.slow_cancels = True
    _force_reason(stack, ExitReason.NEWS, 1)
    stack.quote(150, D(151), t)
    assert manager.manage(position, (), t) is None
    fake.slow_cancels = False
    fake.finish_cancels()
    t += SEC
    stack.quote(0, D(151), t)  # no bid: the exit rests at one tick
    assert manager.flatten(position, t, ExitReason.FAILSAFE) is None
    assert position.exit_order_id is not None
    closed = None
    for _ in range(20):
        t += SEC
        stack.quote(150, D(151), t)
        closed = manager.flatten(position, t, ExitReason.FAILSAFE)
        if closed is not None:
            break
    assert closed is not None
    assert closed.reason is ExitReason.FAILSAFE
