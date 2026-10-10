"""Malik (trade management): watches the open position and gets it out.

Each call to `manage` looks at one fresh quote and decides, in this order:
1. Has the broker's resting stop already filled? Then the trade is closed.
2. Must the position be flat now? Any of:
   - Booker's check says HIT or GAPPED (the bid is at/through the stop or below
     the resting order's limit);
   - the bid has vanished (NO_BID): alarm; the resting stop stays in place;
   - the prime close lead has started or the market is outside prime (G8, NB-07);
   - a news blackout is in force (flat before releases);
   - the news time stop: a trade entered in a news window that is not up
     time_stop_min_gain after time_stop_minutes;
   - the day's realized P&L plus the open trade's net P&L reaches the daily limit;
   - the kill switch is latched.
3. Otherwise, move the trail: Booker updates the stop and the resting order is
   modified to match (never loosened; Booker refuses that).
A STALE quote changes nothing and raises an alarm; the broker's resting stop
protects the position meanwhile.

Exits are limit orders at the bid (NB-12), re-priced to the bid on every call
until filled. The resting stop is cancelled first, so the position is never sold
twice; if the cancel finds the stop already filled, the trade is simply closed.

`reconcile` compares the broker's positions to the ledger after any restart or
doubt (NB-16): on a mismatch that holds still, it cancels every working order,
flattens what the broker holds less what stuck orders already cover, latches the
kill switch (only Andre restarts) and raises an alarm. See `reconcile`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from footprint.core.calendar import SessionKind, entry_cutoff, session_at
from footprint.core.config import Profiles
from footprint.core.models import require_utc
from footprint.decision.session import NewsBlackout, in_news_blackout
from footprint.decision.stop import BidTick, StopCheck, StopKeeper
from footprint.execution.executor.diego import Position, stop_order_ids
from footprint.execution.halt import Halt
from footprint.execution.ledger import Ledger, SoldHold
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    BrokerDown,
    NeverPlaced,
    NotSent,
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    PositionsUncertain,
    Quote,
    Side,
    TrackedBroker,
)

log = logging.getLogger("footprint.trade_mgmt")


class NeedsReconcile(RuntimeError):
    """The broker shows something the manager cannot account for (an unexpected fill
    on the contract). Nothing is booked; the runner must halt and reconcile."""


class ExitReason(Enum):
    STOP = "STOP"
    GAP = "GAP"
    CLOSE = "CLOSE"
    NEWS = "NEWS"
    NEWS_TIME_STOP = "NEWS_TIME_STOP"
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    KILL_SWITCH = "KILL_SWITCH"
    UNPROTECTED = "UNPROTECTED"
    RECONCILE = "RECONCILE"
    FAILSAFE = "FAILSAFE"
    END_OF_DATA = "END_OF_DATA"  # backtest only: the history ran out with a trade open


@dataclass(frozen=True, slots=True)
class Closed:
    position_id: str
    exit_price: Decimal
    exit_ts: datetime
    reason: ExitReason
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class Alarm:
    ts: datetime
    message: str


class Manager:
    def __init__(
        self,
        profiles: Profiles,
        broker: TrackedBroker,
        stops: StopKeeper,
        ledger: Ledger,
        halt: Halt,
        book: QuoteBook,
        on_alarm: Callable[[Alarm], None] | None = None,
    ) -> None:
        self._p = profiles
        self._broker = broker
        self._stops = stops
        self._ledger = ledger
        self._halt = halt
        self._book = book
        self._on_alarm = on_alarm or (lambda alarm: None)
        self._reconcile_closes: list[Closed] = []
        self._exit_reason: dict[str, ExitReason] = {}
        self._exit_attempts: dict[str, int] = {}
        self._stop_stuck_since: dict[str, datetime] = {}
        self._exit_wait_since: dict[str, datetime] = {}  # an order the exit waits on
        self._held_back_alarmed: set[str] = set()
        # A position's stop this system asked the broker to cancel: by an exit under way
        # (cleared if that exit is abandoned), or by reconcile.
        self._exit_cancels: set[str] = set()
        self._reconcile_cancels: set[str] = set()
        self._cancelling_since: dict[str, datetime] = {}  # first seen PendingCancel
        self._exit_started: dict[str, ExitReason] = {}  # why an exit began, before it is out
        self._refusals_seen: dict[str, int] = {}  # cancels of a stop the broker refused
        self._refusal_alarmed: set[str] = set()
        self._escalations: dict[str, int] = {}  # times an exit handed its stop to reconcile
        self._stop_stuck_alarmed: set[str] = set()
        self._quote_alarmed: dict[str, StopCheck] = {}  # stale/no-bid, once per episode
        # A sell of the position the broker reports filled before its price (delta
        # review 2, N-1): position id -> (order id, first seen, why it sold). Nothing is
        # sent for that position until the price arrives or the wait runs out.
        self._pending: dict[str, tuple[str, datetime, ExitReason]] = {}
        self._pending_overdue: set[str] = set()  # CHECK THE FILL alarmed (D40)
        # Contracts held under a durable D40 hold from before this process (G2-1),
        # alarmed CHECK THE FILL once each.
        self._durable_alarmed: set[str] = set()
        # A reconcile order that is not a recorded position's own sell (sent after a
        # restart, or for a stray contract) the broker reports FILLED with its price
        # pending (grind review G4-1): contract id -> (order id, first seen, the id its
        # hold is saved under). Nothing more is sent for that contract meanwhile.
        self._unowned_pending: dict[str, tuple[str, datetime, str]] = {}
        self._unowned_overdue: set[str] = set()  # CHECK THE FILL alarmed, by contract
        self._reconcile_round = 0
        # Names this process's reconcile orders (and their one-cancels-all groups): the
        # rounds restart at 0 after a restart, the names never do (delta review F-7).
        self._run_token = uuid4().hex[:8]
        self._closed: set[str] = set()
        # Reconcile's flatten orders, by the position they were sent for (None after a
        # restart): only a position's own orders can confirm its exit price.
        self._reconcile_orders: dict[str | None, list[str]] = {}
        self._seen: dict[str, tuple[object, datetime]] = {}
        self._reconcile_prices: dict[tuple[str, Side], Decimal] = {}
        self._unpriced_alarmed: set[tuple[str, Side]] = set()  # alarmed: no quote to sell at
        self._alarmed_mismatch: object = None
        self._alarmed_pending: tuple[str, ...] = ()
        self._cancels_stuck_since: datetime | None = None
        self._cancel_escalated = False
        self._acting = False
        self._unbooked_alarmed = False
        self._unrecorded_alarmed: set[str] = set()
        self._unrecorded_named: set[str] = set()  # contracts whose booking id is told
        self._unsettled_since: datetime | None = None
        self._settle_escalated = False
        self._stuck_ids: set[str] = set()

    @property
    def agent_id(self) -> str:
        return "trade_mgmt"

    def manage(
        self,
        position: Position,
        blackouts: Iterable[NewsBlackout],
        now: datetime,
    ) -> Closed | None:
        """One management step on the latest quote in the shared book."""
        require_utc(now, "now")
        quote = self._book.latest_or_empty(position.contract_id)
        pid = position.position_id
        if pid in self._closed:
            raise RuntimeError(f"position {pid!r} is already closed")
        if pid in self._pending:
            return self._await_price(position, now)

        if position.stop_order_id is not None:
            stop_id = position.stop_order_id
            stop_status = self._broker.status(stop_id)
            if not stop_status.cancelling:
                self._cancelling_since.pop(stop_id, None)  # (AEGIS L2-3)
            if stop_status.cancel_refusals > self._refusals_seen.get(stop_id, 0):
                # The broker refused a cancel of the stop (PendingCancel, then acting
                # again). Any PendingCancel from now on is a new cancel.
                self._refusals_seen[stop_id] = stop_status.cancel_refusals
                self._cancelling_since.pop(stop_id, None)
                if stop_status.protects:
                    # Acting again, quote or no quote: no cancel of ours is pending, so
                    # a later cancel by anyone else is theirs (AEGIS M3-1). The exit's
                    # reason and its escalation clock are kept: an exit that keeps
                    # wanting out through refused cancels still escalates after
                    # cancel_escalate_seconds from its first cancel (AEGIS N4-1); they
                    # are cleared only when the exit is dropped (its reason passed).
                    # (Still cancelling: a stale message, or a new cancel; ours stays
                    # ours, AEGIS N4-2.)
                    self._exit_cancels.discard(stop_id)
                    self._reconcile_cancels.discard(stop_id)
                    if pid not in self._refusal_alarmed:
                        self._refusal_alarmed.add(pid)
                        self._alarm(now, f"{pid}: the broker refused a cancel of the resting stop")
            if stop_status.state is OrderState.FILLED:
                if stop_status.avg_price is not None:
                    return self._close(position, stop_status.avg_price, now, ExitReason.STOP)
                return self._fill_pending(position, stop_id, ExitReason.STOP, now)
            if stop_status.held_back:
                # Held back (Inactive at the broker, or being cancelled): no protection
                # (AEGIS N1). Get out through the stop's own cancel, so a cancel that
                # never lands escalates to reconcile and Andre (AEGIS F1).
                if pid not in self._held_back_alarmed:
                    self._held_back_alarmed.add(pid)
                    self._alarm(
                        now, f"{pid}: resting stop held back at the broker; unprotected, flattening"
                    )
                self._now_unprotected(pid)
                return self._start_exit(position, quote, now, ExitReason.UNPROTECTED)
            if stop_status.cancelling:
                # Its cancel is pending at the broker: no protection, but still live (it
                # can still fill), so nothing goes beside it. Our own exit's cancel: the
                # exit carries on (bounded by cancel_escalate_seconds). Anyone else's:
                # unprotected, get out (AEGIS K-1, K-3).
                self._cancelling_since.setdefault(stop_id, now)
                if stop_id in self._exit_cancels or stop_id in self._reconcile_cancels:
                    # Our exit carries on with the reason it began with.
                    started = self._exit_started.get(pid, ExitReason.UNPROTECTED)
                    return self._start_exit(position, quote, now, started)
                if pid not in self._held_back_alarmed:
                    self._held_back_alarmed.add(pid)
                    self._alarm(
                        now,
                        f"{pid}: resting stop being cancelled at the broker, not by this "
                        "system; unprotected, flattening",
                    )
                self._now_unprotected(pid)
                return self._start_exit(position, quote, now, ExitReason.UNPROTECTED)
            if stop_status.state is OrderState.CANCELLED and stop_id in self._exit_cancels:
                # Our own exit's cancel landed: the exit carries on, no false alarm.
                position.stop_order_id = None
                started = self._exit_started.get(pid, ExitReason.UNPROTECTED)
                return self._start_exit(position, quote, now, started)
            if stop_status.state in (OrderState.CANCELLED, OrderState.REJECTED):
                if stop_status.filled > 0:
                    # It sold part of the position before it ended: an exit sized from
                    # a position read that may not show that sale yet could sell what
                    # is not held, and the sale must be booked at its own price, never
                    # at the exit's (grind review G6-1). Reconcile takes over.
                    message = (
                        f"{pid}: stop {stop_id} sold {stop_status.filled} of "
                        f"{stop_status.order.quantity} and is {stop_status.state.value} "
                        "at the broker; reconcile"
                    )
                    self._alarm(now, message)
                    self._stand_down(now, f"{pid}: stop partly filled")
                    raise NeedsReconcile(message)
                # Gone at the broker without this system cancelling it (by hand, or
                # by the broker): the position is unprotected. Get out.
                self._alarm(
                    now,
                    f"{pid}: resting stop {stop_status.state.value} at the broker; "
                    "unprotected, flattening",
                )
                position.stop_order_id = None
                return self._start_exit(position, quote, now, ExitReason.UNPROTECTED)
        if position.exit_order_id is not None:
            return self._work_exit(position, quote, now)
        if position.stop_order_id is None:
            # No resting stop at the broker: get out now, whatever the quote looks like.
            return self._start_exit(position, quote, now, ExitReason.UNPROTECTED)

        tick = BidTick(quote.bid, quote.ask, quote.ts)
        check = self._stops.check(pid, tick, now)
        if check in (StopCheck.STALE, StopCheck.NO_BID):
            if self._past_close(now):
                # Never carry a position past the close for want of a quote (G8, NB-07).
                self._alarm(now, f"{pid}: {check.value} quote at the close; flattening anyway")
                return self._start_exit(position, quote, now, ExitReason.CLOSE)
            if self._quote_alarmed.get(pid) is not check:  # once per episode (AEGIS O5-9)
                self._quote_alarmed[pid] = check
                what = (
                    "QUOTE FEED SILENT: no fresh quote"  # critical (item 9, AEGIS P6-3)
                    if check is StopCheck.STALE
                    else "the bid has vanished"
                )
                self._alarm(now, f"{pid}: {what}; relying on the resting stop")
            return None

        self._quote_alarmed.pop(pid, None)
        reason = self._must_exit(position, quote, check, blackouts, now)
        if reason is not None:
            return self._start_exit(position, quote, now, reason)

        # Managed normally again: an exit begun earlier (whose cancel the broker refused)
        # is abandoned, so a later cancel by anyone else is never taken for ours, and
        # its clocks never count against a later exit (AEGIS L2-2, L2-3).
        self._abandon_exit(position)
        self._stops.on_tick(pid, tick, now)
        self._sync_stop(position, now)
        return None

    def _now_unprotected(self, pid: str) -> None:
        """The stop failed under an exit already under way (held back, cancelled by
        someone else): the exit carries on, but as UNPROTECTED, the reason that now
        holds (its escalation clock is kept)."""
        if pid in self._exit_started:
            self._exit_started[pid] = ExitReason.UNPROTECTED

    def _abandon_exit(self, position: Position) -> None:
        pid = position.position_id
        self._escalations.pop(pid, None)  # a later exit starts its own count
        self._stop_stuck_alarmed.discard(pid)
        self._exit_started.pop(pid, None)
        self._stop_stuck_since.pop(pid, None)
        if position.stop_order_id is not None:
            self._exit_cancels.discard(position.stop_order_id)
            self._reconcile_cancels.discard(position.stop_order_id)

    def flatten(
        self, position: Position, now: datetime, reason: ExitReason = ExitReason.FAILSAFE
    ) -> Closed | None:
        """Get out using the broker and the quote book alone: no stop keeper, no ledger
        read, no news calendar. The runner calls this when normal management keeps
        failing. Call again each step until it returns the close."""
        require_utc(now, "now")
        if position.position_id in self._closed:
            raise RuntimeError(f"position {position.position_id!r} is already closed")
        quote = self._book.latest_or_empty(position.contract_id)
        if position.position_id in self._pending:
            return self._await_price(position, now)
        if position.exit_order_id is not None:
            return self._work_exit(position, quote, now)
        return self._start_exit(position, quote, now, reason)

    def _sync_stop(self, position: Position, now: datetime) -> None:
        """Make the broker's resting stop match Booker's, retrying every call until it
        does. Booker only ever raises the stop, so this never loosens it.
        """
        assert position.stop_order_id is not None
        pid = position.position_id
        spec = self._stops.resting_order(pid)
        try:
            held = self._broker.status(position.stop_order_id)
            if held.state is OrderState.WORKING and held.order.stop_price != spec.trigger:
                self._broker.modify(position.stop_order_id, spec.limit, spec.trigger)
                held = self._broker.status(position.stop_order_id)
                if held.order.stop_price == spec.trigger:
                    position.events.append(f"{now.isoformat()} stop raised to {spec.trigger}")
                else:
                    self._alarm(
                        now,
                        f"{pid}: broker did not raise the resting stop to {spec.trigger} "
                        f"(still {held.order.stop_price}); retrying",
                    )
        except Exception as exc:  # retried on the next call
            self._alarm(now, f"{pid}: could not raise the resting stop: {exc!r}; retrying")

    def _past_close(self, now: datetime) -> bool:
        p = self._p
        cutoff = entry_cutoff(now, p.instrument, p.strategy.console.close_lead_minutes)
        return session_at(now, p.instrument) is not SessionKind.PRIME or now >= cutoff

    def _must_exit(
        self,
        position: Position,
        quote: Quote,
        check: StopCheck,
        blackouts: Iterable[NewsBlackout],
        now: datetime,
    ) -> ExitReason | None:
        p = self._p
        if check is StopCheck.GAPPED:
            return ExitReason.GAP
        if check is StopCheck.HIT:
            return ExitReason.STOP
        if self._past_close(now):
            return ExitReason.CLOSE
        blackouts = tuple(blackouts)
        if in_news_blackout(now, blackouts) is not None:
            return ExitReason.NEWS
        net_open = self._net_pnl(position, quote.bid)
        news = p.execution.news
        window = timedelta(minutes=news.window_minutes)
        in_news_window = any(b.end < position.entry_ts <= b.end + window for b in blackouts)
        if (
            in_news_window
            and now - position.entry_ts >= timedelta(minutes=news.time_stop_minutes)
            and net_open < news.time_stop_min_gain
        ):
            return ExitReason.NEWS_TIME_STOP
        account = self._ledger.account(now)
        if account.realized_pnl_today + net_open <= -p.risk.daily_max_loss:
            return ExitReason.DAILY_LOSS
        if account.realized_pnl_week + net_open <= -p.risk.weekly_max_loss:
            return ExitReason.WEEKLY_LOSS
        if account.kill_switch_latched:
            return ExitReason.KILL_SWITCH
        return None

    def _start_exit(
        self, position: Position, quote: Quote, now: datetime, reason: ExitReason
    ) -> Closed | None:
        pid = position.position_id
        try:
            # What is held is read once, BEFORE the stop comes down: a broker in doubt
            # about its positions keeps the stop, and the exit waits. The exit is sized
            # from this read; nothing of this system's can change what is held after
            # it (the stop and every other order on the contract are confirmed
            # cancelled, unfilled, below), so a later read could only open a window
            # with no stop and no exit.
            try:
                held = self._broker.positions().get(position.contract_id, 0)
            except PositionsUncertain as exc:
                log.warning("%s: positions uncertain (%r); the exit waits", pid, exc)
                return None
            if position.stop_order_id is not None:
                stop_id = position.stop_order_id
                current = self._broker.status(stop_id)
                if not (current.cancelling and stop_id in self._exit_cancels):
                    # (A cancel IBKR already acknowledged is not sent again.) The exit's
                    # reason is recorded once it sends something (AEGIS M3-2).
                    self._exit_started.setdefault(pid, reason)
                    self._exit_cancels.add(stop_id)
                    self._broker.cancel(stop_id)
                cancelled = self._broker.status(stop_id)
                if cancelled.state is OrderState.FILLED:
                    if cancelled.avg_price is not None:
                        return self._close(position, cancelled.avg_price, now, ExitReason.STOP)
                    return self._fill_pending(position, stop_id, ExitReason.STOP, now)
                if cancelled.state is not OrderState.CANCELLED:
                    if cancelled.cancelling:
                        # Acknowledged (PendingCancel): a normal step of an exit, not an
                        # alarm; still bounded by the escalation below (AEGIS L2-4).
                        log.info("%s: resting stop cancel pending at the broker", pid)
                    elif pid not in self._stop_stuck_since:  # once, then the escalation
                        self._alarm(
                            now, f"{pid}: resting stop not cancelled ({cancelled.state.value})"
                        )
                    since = self._stop_stuck_since.setdefault(pid, now)
                    escalate = timedelta(
                        seconds=self._p.execution.reconcile.cancel_escalate_seconds
                    )
                    if now - since >= escalate:
                        # The exit cannot go out beside it: reconcile takes over. If the
                        # stop is held back, reconcile disagrees, halts and chases the
                        # cancel (CANCEL STUCK to Andre); a working stop it agrees with
                        # is managed again.
                        del self._stop_stuck_since[pid]
                        self._escalations[pid] = self._escalations.get(pid, 0) + 1
                        if self._escalations[pid] > 1 and pid not in self._stop_stuck_alarmed:
                            # Reconcile agreed (the stop still protects) and the exit is
                            # still refused: nothing more the system can do safely; Andre
                            # must act (AEGIS O5-1). The exit keeps asking meanwhile.
                            self._stop_stuck_alarmed.add(pid)
                            self._alarm(
                                now,
                                f"{pid}: CANCEL STUCK: the broker will not cancel the resting "
                                "stop (it still protects); the exit cannot go out. Flatten by "
                                "hand or cancel the stop at IBKR",
                            )
                        message = f"{pid}: resting stop will not cancel; reconcile"
                        self._stand_down(now, f"{pid}: stop will not cancel")
                        raise NeedsReconcile(message)
                    return None
                self._stop_stuck_since.pop(pid, None)
                if cancelled.filled > 0:  # part of it sold before the cancel landed
                    message = f"{pid}: stop filled {cancelled.filled} before its cancel; reconcile"
                    self._alarm(now, message)
                    self._stand_down(now, f"{pid}: stop partly filled")
                    raise NeedsReconcile(message)
                position.stop_order_id = None
            else:
                # Unprotected: a stop may have reached the broker unacknowledged, under
                # any id a stop of this position can have (grind review G6-5). If one
                # already filled, the trade is over.
                orphans = []
                for stop_id in stop_order_ids(pid):
                    with contextlib.suppress(KeyError):
                        orphan = self._broker.status(stop_id)
                        if orphan.state is OrderState.FILLED:
                            orphans.append(orphan)
                if len(orphans) > 1:
                    # Sold twice: no one fill is its exit, and nothing more is sold.
                    message = (
                        f"{pid}: its stops {[o.order.order_id for o in orphans]} both "
                        "filled: sold twice. Nothing more is sold; check the account and "
                        "flatten by hand, then book it with Ledger.book_by_hand"
                    )
                    self._alarm(now, message)
                    self._stand_down(now, f"{pid}: sold twice")
                    raise NeedsReconcile(message)
                if orphans:
                    orphan = orphans[0]
                    if orphan.avg_price is not None:
                        return self._close(position, orphan.avg_price, now, ExitReason.STOP)
                    return self._fill_pending(position, orphan.order.order_id, ExitReason.STOP, now)
            # Whatever else is open on this contract (an unacknowledged stop, another
            # path's sell) is cancelled and confirmed first, so the exit can never sit
            # beside a second seller.
            # Every order the system sent or the broker lists, not the list alone (a
            # live broker can list a new order late).
            for order_id, live in self._broker.working().items():
                if live.order.contract_id != position.contract_id:
                    continue
                order = live.order
                try:
                    self._broker.cancel(order_id)
                    gone = self._broker.status(order_id)
                except BrokerDown:
                    raise  # an outage, not a fault (D35): reconcile once IBKR is back
                except Exception as exc:
                    # Its fate cannot be confirmed (sent, answer lost, broker silent):
                    # it may still sell. Nothing is sent beside it; reconcile takes
                    # over, with its bounded escalation to Andre.
                    message = f"{pid}: cannot confirm {order_id} ({exc!r}); reconcile"
                    self._alarm(now, message)
                    self._stand_down(now, f"{pid}: order outcome unknown")
                    raise NeedsReconcile(message) from exc
                if gone.filled > 0:
                    if (
                        gone.state is OrderState.FILLED
                        and gone.order.side is Side.SELL
                        and gone.filled == position.contracts
                    ):  # it sold exactly this position: the trade is over
                        if gone.avg_price is not None:
                            return self._close(position, gone.avg_price, now, ExitReason.STOP)
                        return self._fill_pending(position, order_id, ExitReason.STOP, now)
                    # Anything else (a buy, a partial, another size) changes what is
                    # held in a way this position cannot book: stop and reconcile.
                    message = (
                        f"{pid}: {order.order_id} filled {gone.filled} "
                        f"{gone.order.side.value} during the cancel; reconcile"
                    )
                    self._alarm(now, message)
                    self._stand_down(now, f"{pid}: unexpected fill on the contract")
                    raise NeedsReconcile(message)
                if gone.state is OrderState.WORKING:
                    # Bounded like the stop's own cancel (AEGIS F1): never waits forever.
                    if order_id not in self._exit_wait_since:
                        self._alarm(now, f"{pid}: {order.order_id} not cancelled yet; exit waits")
                    since = self._exit_wait_since.setdefault(order_id, now)
                    escalate = timedelta(
                        seconds=self._p.execution.reconcile.cancel_escalate_seconds
                    )
                    if now - since >= escalate:
                        del self._exit_wait_since[order_id]
                        message = f"{pid}: {order.order_id} will not cancel; reconcile"
                        self._stand_down(now, f"{pid}: {order.order_id} will not cancel")
                        raise NeedsReconcile(message)
                    return None
                self._exit_wait_since.pop(order_id, None)
        except (NeedsReconcile, BrokerDown):
            raise  # BrokerDown: an outage is not a halt (D35; independent audit A-6)
        except Exception as exc:  # the broker failed mid-exit: halt, retry next call
            self._alarm(now, f"{pid}: broker error while exiting: {exc!r}; retrying")
            self._stand_down(now, f"{pid}: broker error while exiting")
            return None
        self._exit_reason[pid] = reason
        if reason in (
            ExitReason.GAP,
            ExitReason.UNPROTECTED,
            ExitReason.KILL_SWITCH,
            ExitReason.FAILSAFE,
        ):
            # Without its stop the position is unprotected until this exit fills: that
            # rings the phone (independent audit A-4; "no resting stop" is critical).
            unprotected = "; no resting stop" if reason is ExitReason.UNPROTECTED else ""
            self._alarm(now, f"{pid}: flattening ({reason.value}){unprotected}")
        return self._place_exit(position, quote, now, reason, held)

    def _place_exit(
        self, position: Position, quote: Quote, now: datetime, reason: ExitReason, held: int
    ) -> Closed | None:
        """`held`: what the broker held before the stop came down (see _start_exit)."""
        pid = position.position_id
        unpriced = self._unpriced_sell(position)
        if unpriced is not None:
            # One of this position's sells already filled, price not reported yet: the
            # position is sold. Never a second sell beside it (N-1).
            return self._fill_pending(position, unpriced, reason, now)
        if 0 < held != position.contracts:
            # More (or fewer) held than this position: selling its size would leave
            # contracts nobody manages, or sell short. Stop and reconcile.
            message = f"{pid}: broker holds {held}, position is {position.contracts}; reconcile"
            self._alarm(now, message)
            self._stand_down(now, f"{pid}: held size differs from the position")
            raise NeedsReconcile(message)
        quantity = min(position.contracts, held)
        if quantity <= 0:
            # The broker reports nothing held. Book it only on a fill the broker
            # confirms by id; one position read alone may lag or flicker, and the
            # ledger never takes a guessed price.
            price, found = self._actual_exit(position)
            if found:
                self._alarm(now, f"{pid}: already flat at the broker; booking {price}")
                return self._close(position, price, now, reason)
            message = f"{pid}: broker reports flat but no fill confirms it; reconcile"
            self._alarm(now, message)
            self._stand_down(now, f"{pid}: flat without a confirmed fill")
            raise NeedsReconcile(message)
        self._exit_attempts[pid] = self._exit_attempts.get(pid, 0) + 1
        tick = self._p.instrument.option_tick_size
        # A sell limit at the bid; with no bid, at one tick (the floor of any sale).
        price = quote.bid if quote.bid > 0 else tick
        order = Order(
            order_id=f"{pid}-exit-{self._exit_attempts[pid]}",
            contract_id=position.contract_id,
            side=Side.SELL,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            limit_price=price,
            oca_group=pid,  # with its stop: the broker lets only one of them fill
        )
        position.exit_order_id = order.order_id
        position.events.append(f"{now.isoformat()} exit {reason.value} at {price}")
        try:
            status = self._broker.place(order)
        except NotSent:
            # Refused before anything reached the broker: there is no exit. Forget it,
            # so the next step sees the position as it is (the stop already down) and
            # gets out again; never followed as a working exit that does not exist.
            position.exit_order_id = None
            position.events.append(f"{now.isoformat()} exit {order.order_id} not sent")
            raise
        except Exception as exc:
            # The answer is lost, not necessarily the order: it may be working at the
            # broker. Keep its id (it is recorded and followed until confirmed
            # finished) and reconcile; never send another exit beside it blind.
            message = f"{pid}: exit {order.order_id} sent but the answer was lost: {exc!r}"
            self._alarm(now, message)
            self._stand_down(now, f"{pid}: exit outcome unknown")
            raise NeedsReconcile(message) from exc
        if status.state is OrderState.FILLED:
            if status.avg_price is not None:
                return self._close(position, status.avg_price, now, reason)
            return self._fill_pending(position, order.order_id, reason, now)
        if status.state is not OrderState.WORKING:
            self._alarm(now, f"{pid}: exit {status.state.value}; retrying and halting entries")
            position.exit_order_id = None
            self._stand_down(now, f"{pid}: exit {status.state.value}")
        return None

    def _work_exit(self, position: Position, quote: Quote, now: datetime) -> Closed | None:
        assert position.exit_order_id is not None
        reason = self._exit_reason.get(position.position_id, ExitReason.STOP)
        try:
            return self._work_exit_once(position, quote, now, reason)
        except (NeedsReconcile, BrokerDown):
            raise  # BrokerDown: an outage is not a halt (D35; independent audit A-6)
        except Exception as exc:  # broker error: halt, keep the exit recorded, retry
            self._alarm(now, f"{position.position_id}: broker error working the exit: {exc!r}")
            self._stand_down(now, f"{position.position_id}: broker error working the exit")
            return None

    def _work_exit_once(
        self, position: Position, quote: Quote, now: datetime, reason: ExitReason
    ) -> Closed | None:
        assert position.exit_order_id is not None
        try:
            status = self._broker.status(position.exit_order_id)
        except NeverPlaced:
            self._alarm(now, f"{position.position_id}: exit never reached the broker; retrying")
            position.exit_order_id = None
            return self._start_exit(position, quote, now, reason)
        if status.state is OrderState.FILLED:
            if status.avg_price is not None:
                return self._close(position, status.avg_price, now, reason)
            return self._fill_pending(position, position.exit_order_id, reason, now)
        if status.state is not OrderState.WORKING:
            self._alarm(now, f"{position.position_id}: exit {status.state.value}; retrying")
            self._stand_down(now, f"{position.position_id}: exit {status.state.value}")
            position.exit_order_id = None
            # Through the same sweep as any exit: never re-place beside another seller.
            return self._start_exit(position, quote, now, reason)
        tick = self._p.instrument.option_tick_size
        target = quote.bid if quote.bid > 0 else tick
        if self._stale(quote, now):
            # No fresh price at the close: walk the limit down a tick per call.
            target = max(tick, status.order.limit_price - tick)
        if target < status.order.limit_price:
            self._broker.modify(position.exit_order_id, target, None)
            status = self._broker.status(position.exit_order_id)
            if status.state is OrderState.FILLED:
                if status.avg_price is not None:
                    return self._close(position, status.avg_price, now, reason)
                return self._fill_pending(position, position.exit_order_id, reason, now)
        return None

    def _stale(self, quote: Quote, now: datetime) -> bool:
        age = now - quote.ts
        return age > timedelta(seconds=self._p.execution.quotes.max_age_seconds) or quote.bid <= 0

    def _stand_down(self, now: datetime, reason: str) -> None:
        """Halt in memory first (cannot fail), then latch the durable kill switch."""
        self._halt.trip(reason)
        try:
            self._ledger.latch_kill_switch(reason)
        except Exception as exc:  # the in-memory halt already holds
            self._alarm(now, f"could not save the kill-switch latch: {exc!r}")

    def _net_pnl(self, position: Position, price: Decimal) -> Decimal:
        pv = self._p.instrument.point_value
        fees = 2 * self._p.risk.fee_per_contract_per_side * position.contracts
        return (price - position.entry_price) * pv * position.contracts - fees

    def _close(
        self, position: Position, price: Decimal, now: datetime, reason: ExitReason
    ) -> Closed:
        net = self._net_pnl(position, price)
        if self._ledger.open_position_id == position.position_id:
            self._ledger.on_trade_closed(now, position.position_id, net)  # releases holds
        else:
            self._alarm(now, f"{position.position_id}: closed but the ledger never recorded it")
            try:  # the sale is confirmed at its real price: its D40 hold goes with it
                self._ledger.release_sold(position.position_id)
            except Exception as exc:  # a hold left in place only ever blocks a sell
                self._alarm(now, f"{position.position_id}: could not release its hold: {exc!r}")
        with contextlib.suppress(KeyError):  # Booker never held a stop (unprotected case)
            self._stops.close(position.position_id)
        self._closed.add(position.position_id)
        self._pending.pop(position.position_id, None)
        self._pending_overdue.discard(position.position_id)
        position.events.append(f"{now.isoformat()} closed {reason.value} at {price}, net {net}")
        log.info("closed %s at %s (%s), net %s", position.position_id, price, reason.value, net)
        return Closed(position.position_id, price, now, reason, net)

    def route_alarms(self, sink: Callable[[Alarm], None]) -> None:
        """Send every alarm to `sink` (the runner owns the one line to Andre)."""
        self._on_alarm = sink

    def take_reconcile_closes(self) -> list[Closed]:
        """Trades reconcile booked since the last call (for the journal)."""
        closes, self._reconcile_closes = self._reconcile_closes, []
        return closes

    def _alarm(self, now: datetime, message: str) -> None:
        log.critical(message)
        try:
            self._on_alarm(Alarm(now, message))
        except Exception:  # an alarm must never stop the safety action it announces
            log.exception("could not deliver an alarm")

    def reconcile(self, position: Position | None, now: datetime) -> bool:
        """True if the broker, the ledger and `position` all agree. Call again every
        step until it returns True. The broker's order LIST is never trusted alone (a
        live broker can list a new order late): every order the system itself sent is
        followed by its id through `status`, and one that cannot be confirmed finished
        is counted as still working. On a disagreement, each round:

        1. Settle: before the first action the disagreement must hold still for
           `settle_seconds`; before any flatten, what is held must too. A report that
           will not hold still for `max_settle_seconds` escalates to Andre and halts;
           it is never acted on unsettled (a sell on an untrusted report can go short).
        2. Alarm (when holdings or the ledger change), halt, and cancel every working
           order: those the broker lists and those the system sent. A cancel that
           fails or is not confirmed is chased every round, and escalates once after
           `cancel_escalate_seconds`. A stuck order on the reducing side is re-priced
           toward the market instead (never beyond what is held).
        3. Post a limit order for what is held, less what still-working orders on the
           same side cover, so the flatten never sells (or buys) twice. Priced at the
           touch on a fresh quote; on a stale or missing quote the price walks one tick
           a round from the last price, never resting on a dead quote.
        4. Latch the kill switch.

        Booker's stops live in memory. After a restart, pass `position=None` (nothing
        the system can manage), so anything the broker still holds is flattened rather
        than run without its stop and trail.
        """
        require_utc(now, "now")
        expected = {position.contract_id: position.contracts} if position else {}
        # A working exit (with its stop already cancelled) is a known state, not a doubt.
        expected_orders = (
            {o for o in (position.stop_order_id, position.exit_order_id) if o is not None}
            if position
            else set()
        )
        ledger_open = self._ledger.open_position_id
        ledger_ok = ledger_open == (position.position_id if position else None)
        try:
            actual = self._broker.positions()
        except PositionsUncertain:
            # Not a failure: the report is not settled yet (bounded by _unsettled).
            self._unsettled(now, "the broker's positions")
            return False
        working = self._working_orders(position)
        working_ids = set(working)
        # A held-back stop (Inactive at the broker, or stuck being cancelled) is not the
        # position's protection: never "agreed" with (AEGIS I-1). The disagreement path
        # then halts, chases its cancel (CANCEL STUCK to Andre) and flattens around it.
        expected_orders -= {oid for oid, status in working.items() if status.held_back}
        # A stop whose cancel is pending is still live (it can fill), and a normal exit
        # passes through PendingCancel: agreed with for cancel_escalate_seconds, then
        # no longer (AEGIS J-1, K-1).
        if position is not None and position.stop_order_id in self._exit_cancels:
            # Our own exit's cancel of the stop landed (nothing filled): the stop is
            # expected gone, and management carries the exit on (AEGIS L2-1).
            with contextlib.suppress(KeyError):
                gone = self._broker.status(position.stop_order_id)
                if gone.state is OrderState.CANCELLED and gone.filled == 0:
                    expected_orders.discard(position.stop_order_id)
        escalate = timedelta(seconds=self._p.execution.reconcile.cancel_escalate_seconds)
        for oid, status in working.items():
            if status.cancelling:
                if now - self._cancelling_since.setdefault(oid, now) >= escalate:
                    expected_orders.discard(oid)
            else:
                self._cancelling_since.pop(oid, None)
        if actual == expected and ledger_ok and working_ids == expected_orders:
            self._reset_reconcile()
            return True
        mismatch = (tuple(sorted(actual.items())), tuple(sorted(working_ids)), ledger_open)
        if not self._acting:
            # Only the first action waits for the disagreement to hold still; once
            # reconcile is acting, its own cancels change the picture, and the wait
            # on what is held (below) guards every flatten.
            if not self._settled("mismatch", mismatch, now):
                self._unsettled(now, "the disagreement")
                return False
            self._unsettled_since = None
            self._acting = True
        # Alarm when what is held or booked changes, not when reconcile's own cancels
        # and re-prices change the order list.
        alarm_key = (tuple(sorted(actual.items())), ledger_open)
        if alarm_key != self._alarmed_mismatch:
            self._alarmed_mismatch = alarm_key
            self._alarm(
                now,
                f"reconcile mismatch: broker {actual} with orders {sorted(working_ids)}, "
                f"expected {expected} with {sorted(expected_orders)}, ledger open {ledger_open}",
            )
        self._halt.trip("unclear recovery: broker, ledger and records disagree")
        for order_id in working:
            try:
                self._reconcile_cancels.add(order_id)
                self._broker.cancel(order_id)
            except Exception as exc:  # chased again next round
                log.critical("reconcile could not cancel %s: %r", order_id, exc)
        pending = list(self._working_orders(position).values())
        self._watch_stuck_cancels(pending, now)
        try:
            remaining = self._broker.positions()  # what is really held after the cancels
        except PositionsUncertain:
            self._unsettled(now, "the broker's positions")
            self._stand_down(now, "unclear recovery")
            return False
        held = (
            tuple(sorted(remaining.items())),
            tuple(sorted(status.order.order_id for status in pending)),
        )
        if not self._settled("remaining", held, now):
            self._unsettled(now, "what the broker holds")
            self._stand_down(now, "unclear recovery")
            return False
        self._unsettled_since = None
        self._reconcile_round += 1
        stuck_before = self._stuck_ids
        self._stuck_ids = {status.order.order_id for status in pending}
        for contract_id, qty in remaining.items():
            if self._sold_unpriced(position, contract_id, now):
                continue  # already sold: what is held has not caught up yet (N-1)
            side = Side.SELL if qty > 0 else Side.BUY
            if ledger_open is None and contract_id not in self._unrecorded_alarmed:
                # Held at the broker with no open position in the ledger (the process
                # died, or the ledger's write failed, between the fill and the record):
                # however it is flattened (by reconcile or by an order already working),
                # its P&L will not be booked.
                self._unrecorded_alarmed.add(contract_id)
                self._alarm(
                    now,
                    f"flattening {qty} of {contract_id} that the ledger never recorded: its P&L "
                    "is not booked (daily loss and drawdown are understated). Book this round "
                    "trip once, with Ledger.book_by_hand from the broker statement, under the "
                    "id named when the first reconcile order for it is sent "
                    f"(reconcile-...-{contract_id}; if an order already working covers it, "
                    "that order's id, named then), or the one a later hold names: never a "
                    "second id",
                )
            reducing = [
                status
                for status in pending
                if status.order.contract_id == contract_id and status.order.side is side
            ]
            covered = sum(status.order.quantity - status.filled for status in reducing)
            needed = abs(qty) - covered
            if covered > abs(qty):
                self._alarm(
                    now,
                    f"stuck orders on {contract_id} would {side.value} {covered} against "
                    f"{abs(qty)} held: flatten by hand",
                )
            price = self._reconcile_price(contract_id, side, now)
            if price is None:
                if (contract_id, side) not in self._unpriced_alarmed:
                    self._unpriced_alarmed.add((contract_id, side))
                    self._alarm(
                        now,
                        f"cannot price the {side.value} of {abs(qty)} {contract_id}: no quote "
                        "seen yet. Nothing is sent until one arrives; flatten by hand if it "
                        "does not",
                    )
                continue
            self._reprice_stuck(reducing, stuck_before, abs(qty), price)
            if needed <= 0:
                # Orders that would not cancel already cover it. No reconcile order is
                # sent, so the id to book an unrecorded round trip under is the covering
                # order's, told once (grind review G13-1).
                if reducing and ledger_open is None and contract_id not in self._unrecorded_named:
                    self._unrecorded_named.add(contract_id)
                    cover = reducing[0].order.order_id
                    self._alarm(
                        now,
                        f"{cover} (still working, its cancel not confirmed) covers the "
                        f"{side.value} of {abs(qty)} {contract_id}, a holding with no position "
                        "in the ledger: no reconcile order is sent for it. Book this round "
                        f"trip once, with Ledger.book_by_hand as {cover}, at the statement's "
                        "net P&L, unless a later alarm names a hold under a reconcile order "
                        f"for {contract_id} (then as that one): never book it twice",
                    )
                continue
            order_id = f"reconcile-{self._reconcile_round}-{self._run_token}-{contract_id}"
            order = Order(
                order_id=order_id,
                contract_id=contract_id,
                side=side,
                quantity=needed,
                order_type=OrderType.LIMIT,
                limit_price=price,
                # Its own one-cancels-all group: a second live copy IBKR makes of it (a
                # modify under degraded connectivity) carries the same group, so the two
                # can never both fill and go short (independent audit A-7). One group per
                # order, never reused, also across restarts (the run token).
                oca_group=order_id,
            )
            owner = position.position_id if position is not None else None
            self._reconcile_orders.setdefault(owner, []).append(order.order_id)
            self._broker.place(order)  # recorded and followed by id from now on
            if ledger_open is None and contract_id not in self._unrecorded_named:
                # The id to book the unrecorded round trip under, told once, when the
                # first reconcile order for it is sent: otherwise only a late price would
                # ever name one (grind review G12-1).
                self._unrecorded_named.add(contract_id)
                self._alarm(
                    now,
                    f"{order_id} (reconcile) sent to {side.value} {needed} of {contract_id}, "
                    "a holding with no position in the ledger. Book this round trip once, "
                    f"with Ledger.book_by_hand as {order_id}, at the statement's net P&L, "
                    "unless a later alarm names a hold under another reconcile order for "
                    f"{contract_id} (then as that one): never book it twice",
                )
        if not remaining and position is not None and ledger_open == position.position_id:
            price, actual_fill = self._actual_exit(position)
            if actual_fill:
                self._reconcile_closes.append(
                    self._close(position, price, now, ExitReason.RECONCILE)
                )
            elif not self._unbooked_alarmed:
                # Flat (and settled), but no fill confirms what closed it: the ledger
                # never takes a guessed price. It stays open; the system stays dark.
                self._unbooked_alarmed = True
                parts = self._sold_in_parts(position)
                how = (
                    f"it was sold in parts ({parts}), so no one fill gives its exit price"
                    if parts is not None
                    else "no fill confirms the exit price"
                )
                self._alarm(
                    now,
                    f"{position.position_id}: flat at the broker but {how}: book it with "
                    "Ledger.book_by_hand from the broker statement",
                )
        elif not remaining and ledger_open is not None and position is None:
            self._alarm(
                now,
                f"ledger shows {ledger_open} open but nothing is held and no record survives: "
                "book it with Ledger.book_by_hand",
            )
        self._stand_down(now, "unclear recovery: broker, ledger and records disagree")
        return False

    def _fill_pending(
        self, position: Position, order_id: str, reason: ExitReason, now: datetime
    ) -> Closed | None:
        """`order_id`, a sell of this position, is filled at the broker but its price is
        not reported yet (IBKR's status can run ahead of its execution report; delta
        review 2, N-1). The position is sold: nothing more is sent for it, and nothing
        is booked until the real price arrives. Past `fill_price_wait_seconds`: alarm,
        halt and reconcile (reconcile never sells it again either; see `_sold_unpriced`)."""
        pid = position.position_id
        known = self._pending.get(pid)
        if known is None or known[0] != order_id:
            known = self._pending[pid] = (order_id, now, reason)
            self._hold_durably(SoldHold(pid, position.contract_id, order_id), now)
            log.warning(
                "%s: %s filled at the broker; its price is not reported yet: nothing is "
                "sent until it is",
                pid,
                order_id,
            )
        wait = timedelta(seconds=self._p.execution.reconcile.fill_price_wait_seconds)
        if now - known[1] >= wait:
            message = (
                f"{pid}: {order_id} filled at the broker but its price has not been reported "
                f"since {known[1].isoformat()}; reconcile"
            )
            self._alarm(now, message)
            self._stand_down(now, f"{pid}: fill price not reported")
            raise NeedsReconcile(message)
        return None  # waiting: the position is sold, its close is booked on the price

    def _await_price(self, position: Position, now: datetime) -> Closed | None:
        """A sell of this position filled with its price pending: book it once the
        price arrives (never a guess), and send nothing meanwhile. Booked only when that
        one sell sold the whole position (`_actual_exit`): a position sold in parts, or
        sold twice, has no one exit price, so nothing is booked, Andre is told each
        sale, the kill switch is latched and reconcile takes over (grind review G11-1;
        the G7-1 rule on this path)."""
        pid = position.position_id
        order_id, _, reason = self._pending[pid]
        status = self._broker.status(order_id)
        if status.state is OrderState.FILLED and status.avg_price is not None:
            price, whole = self._actual_exit(position)
            if whole:
                return self._close(position, price, now, reason)
            parts = self._sold_in_parts(position)
            how = (
                f"it was sold in parts ({parts}), so no one fill gives its exit price"
                if parts is not None
                else f"no one fill confirms its exit price ({order_id} filled)"
            )
            message = (
                f"{pid}: {how}. Nothing more is sold; book it with Ledger.book_by_hand "
                "from the broker statement"
            )
            if not self._unbooked_alarmed:
                self._unbooked_alarmed = True
                self._alarm(now, message)
            self._stand_down(now, f"{pid}: sold in parts, book it by hand")
            raise NeedsReconcile(message)
        return self._fill_pending(position, order_id, reason, now)

    def _sell_candidates(self, position: Position) -> list[str]:
        """Every order id that may be a sell of this position (its pending sell, its
        stop, its exits, reconcile orders sent for it), newest first, each once."""
        pending = self._pending.get(position.position_id)
        candidates = [pending[0] if pending else None, position.stop_order_id]
        candidates += reversed(stop_order_ids(position.position_id))  # G6-5
        candidates += [
            f"{position.position_id}-exit-{n}"
            for n in range(self._exit_attempts.get(position.position_id, 0), 0, -1)
        ]
        candidates += list(reversed(self._reconcile_orders.get(position.position_id, [])))
        return list(dict.fromkeys(o for o in candidates if o is not None))

    def _sells_that_sold(self, position: Position) -> list[OrderStatus]:
        """The broker's answer for each sell of this position that sold any of it."""
        sold = []
        for order_id in self._sell_candidates(position):
            try:
                status = self._broker.status(order_id)
            except KeyError:
                continue
            if (
                status.filled > 0
                and status.order.side is Side.SELL
                and status.order.contract_id == position.contract_id
            ):
                sold.append(status)
        return sold

    def _unpriced_sell(self, position: Position) -> str | None:
        """The id of a sell of this position (its stop, an exit, a reconcile order sent
        for it) that the broker reports has sold some of it with no price yet; None if
        there is none. Filled in part counts as well as FILLED: a stop that sold 1 of 2
        and was then cancelled has sold, whatever its final state, and a second sell
        beside it while the report lags can go short (grind review G7-1)."""
        for status in self._sells_that_sold(position):
            if status.avg_price is None:
                return status.order.order_id
        return None

    def _sold_unpriced(self, position: Position | None, contract_id: str, now: datetime) -> bool:
        """Reconcile: this position's holding on `contract_id` is already sold by a fill
        the broker reported FILLED with its price pending (what is held lags it). Never
        sold again, inside the wait or past it: a second sell beside a known filled sell
        can go short (N3-2, D40). Past the wait the kill switch is latched and Andre gets
        one critical CHECK THE FILL alarm; the sale is booked if its price and the new
        holding ever arrive, otherwise he flattens by hand. Fails closed, like CANCEL
        STUCK. The hold is also kept in the ledger, so a restart (`reconcile(None)`)
        never sells beside the filled sell either (G2-1; see `_held_from_before`)."""
        if position is None or contract_id != position.contract_id:
            beside = position.contract_id if position is not None else None
            return self._sold_unowned(contract_id, now, beside)
        pid = position.position_id
        if pid not in self._pending:
            unpriced = self._unpriced_sell(position)
            if unpriced is None:
                return self._sold_unowned(contract_id, now, None)
            reason = self._exit_reason.get(pid, ExitReason.RECONCILE)
            self._pending[pid] = (unpriced, now, reason)
            self._hold_durably(SoldHold(pid, position.contract_id, unpriced), now)
        order_id, since, _ = self._pending[pid]
        wait = timedelta(seconds=self._p.execution.reconcile.fill_price_wait_seconds)
        if now - since < wait:
            log.warning("reconcile: %s already sold by %s (price pending)", contract_id, order_id)
            return True
        if pid not in self._pending_overdue:
            self._pending_overdue.add(pid)
            self._alarm(
                now,
                f"{pid}: CHECK THE FILL: {order_id} sold this position at the broker "
                "(FILLED, in full or in part) but its price never came, and the broker "
                f"still reports {contract_id} held. Nothing more is sold (a second sell "
                "could go short); the kill switch is latched. Check the fill in the "
                "broker's own platform and flatten by hand only what is really held",
            )
        self._stand_down(now, f"{pid}: {order_id} filled, price and holding not reported")
        return True

    def _hold_durably(self, hold: SoldHold, now: datetime) -> None:
        """Save the D40 hold in the ledger. If it cannot be saved, the in-memory hold
        still stops every sell in this process, but a restart would not know it: halt,
        latch and tell Andre not to restart (fail closed)."""
        try:
            self._ledger.hold_sold(hold)
        except Exception as exc:
            self._alarm(
                now,
                f"{hold.position_id}: could not save the hold on {hold.contract_id} "
                f"({hold.order_id} FILLED, price pending): {exc!r}. Nothing more is sold in "
                "this process; do not restart the bot until the fill is checked by hand",
            )
            self._stand_down(now, f"{hold.position_id}: hold on {hold.order_id} not saved")

    def _unpriced_reconcile_order(self, contract_id: str) -> str | None:
        """The newest reconcile order this process sent for `contract_id`, under any
        owner and on either side, that the broker reports has filled (in full or in
        part, whatever its final state) with no price yet."""
        for order_ids in self._reconcile_orders.values():
            for order_id in reversed(order_ids):
                try:
                    status = self._broker.status(order_id)
                except KeyError:
                    continue
                if (
                    status.filled > 0  # FILLED or filled in part (G7-1)
                    and status.avg_price is None
                    and status.order.contract_id == contract_id
                ):
                    return order_id
        return None

    def _sold_unowned(self, contract_id: str, now: datetime, beside: str | None) -> bool:
        """Reconcile: what is held on `contract_id` was already sold (or bought back) by
        a reconcile order that is not a recorded position's own sell (sent after a
        restart, or for a stray contract), FILLED with its price pending (grind review
        G4-1). Never sent again while the report lags: a second order could go short.
        The hold is saved under the ledger's open position (booking it releases the
        hold), or under the order's own id when none is open (Andre's
        `Ledger.book_by_hand` releases it). It is kept even once the order's price
        arrives: a report that still shows the contract held after a priced fill is
        not acted on either (fail closed). Past `fill_price_wait_seconds`: latch, halt
        and one CHECK THE FILL alarm, as for the position's own sell (D40). `beside`:
        the contract of the recorded position open beside it, when this is a stray
        holding of another contract (that position must never be booked by hand for
        it, G5-2)."""
        known = self._unowned_pending.get(contract_id)
        if known is None:
            order_id = self._unpriced_reconcile_order(contract_id)
            if order_id is None:
                return self._held_from_before(contract_id, now)
            hold_id = self._ledger.open_position_id or order_id
            known = self._unowned_pending[contract_id] = (order_id, now, hold_id)
            self._hold_durably(SoldHold(hold_id, contract_id, order_id), now)
            if hold_id == order_id:
                # Only Andre's booking releases it: told now, never silent (G5-1).
                self._alarm(
                    now,
                    f"{order_id} (reconcile) filled {contract_id} at the broker with no "
                    "position in the ledger, its price pending: a hold is saved under "
                    f"{order_id}. Until it is released, reconcile sends nothing on "
                    f"{contract_id}. Book this round trip once, with Ledger.book_by_hand as "
                    f"{order_id}, at the statement's net P&L: that releases the hold (the "
                    "same round trip as the 'never recorded' alarm: never book it twice)",
                )
        order_id, since, hold_id = known
        wait = timedelta(seconds=self._p.execution.reconcile.fill_price_wait_seconds)
        if now - since < wait:
            log.warning("reconcile: %s already sold by %s (price pending)", contract_id, order_id)
            return True
        if contract_id not in self._unowned_overdue:
            self._unowned_overdue.add(contract_id)
            if hold_id == order_id:
                # No position in the ledger: only Andre's booking releases it (G5-2).
                release = (
                    "The hold is released when you book this round trip once, with "
                    f"Ledger.book_by_hand as {hold_id}, at the statement's net P&L. It is "
                    f"the same round trip as the 'never recorded' alarm for {contract_id}: "
                    "never book it twice"
                )
            elif beside is not None and beside != contract_id:
                # A stray beside the open position: booking that position by hand while
                # it is held would close it in the ledger (G5-2).
                release = (
                    f"The hold is kept under the open position {hold_id} and released when "
                    f"that position is booked; do not book {hold_id} by hand while it is "
                    "still held"
                )
            else:
                release = (
                    f"The hold is kept under the open position {hold_id} and released when "
                    f"it is booked. If {contract_id} is that position's contract and none of "
                    f"it is still held, book it once, with Ledger.book_by_hand as {hold_id}, "
                    "at the statement's net P&L (never book it twice); otherwise do not book "
                    "it by hand"
                )
            self._alarm(
                now,
                f"CHECK THE FILL: {order_id} (reconcile) filled {contract_id} at the broker "
                f"(FILLED, price pending when seen), and the broker still reports "
                f"{contract_id} held. Nothing more is sent for it (a second order could go "
                "short); the kill switch is latched. Check the fill in the broker's own "
                f"platform and flatten by hand only what is really held. {release}",
            )
        self._stand_down(now, f"{contract_id}: {order_id} filled, price and holding not reported")
        return True

    def _held_from_before(self, contract_id: str, now: datetime) -> bool:
        """Reconcile: `contract_id` is under a durable D40 hold this process did not
        see happen (it was restarted, or lost its record): a sell the broker reported
        FILLED with its price pending already sold it. Never sold again. Latch, halt and
        tell Andre once; the hold stays until the sale is booked (its real price, or
        Andre's `Ledger.book_by_hand`)."""
        holds = [h for h in self._ledger.sold_unpriced if h.contract_id == contract_id]
        if not holds:
            return False
        orders = ", ".join(h.order_id for h in holds)
        if contract_id not in self._durable_alarmed:
            self._durable_alarmed.add(contract_id)
            self._alarm(
                now,
                f"CHECK THE FILL: {orders} sold {contract_id} at the broker (FILLED, price "
                f"pending) before this restart, and the broker still reports {contract_id} "
                "held. Nothing more is sold (a second sell could go short); the kill switch "
                "is latched. Check the fill in the broker's own platform, flatten by hand "
                "only what is really held, then book the position with Ledger.book_by_hand",
            )
        self._stand_down(now, f"{contract_id}: {orders} filled before a restart, price pending")
        return True

    def _settled(self, key: str, snapshot: object, now: datetime) -> bool:
        """True once `snapshot` has been seen unchanged for settle_seconds."""
        settle = timedelta(seconds=self._p.execution.reconcile.settle_seconds)
        seen = self._seen.get(key)
        if seen is None or seen[0] != snapshot:
            self._seen[key] = (snapshot, now)
            return settle <= timedelta(0)
        return now - seen[1] >= settle

    def _unsettled(self, now: datetime, what: str) -> None:
        """Still waiting for the broker's report to hold still: bounded. The clock
        runs over one unbroken stretch of unsettled rounds (a settled round resets
        it), so a long, normal reconcile never reads as an unsettled report."""
        if self._unsettled_since is None:
            self._unsettled_since = now
        limit = timedelta(seconds=self._p.execution.reconcile.max_settle_seconds)
        log.warning("reconcile: waiting for %s to hold still", what)
        if not self._settle_escalated and now - self._unsettled_since >= limit:
            self._settle_escalated = True
            self._alarm(
                now,
                f"BROKER REPORT NOT SETTLING since {self._unsettled_since.isoformat()}: "
                f"{what} keeps changing. Nothing is sold on an untrusted report; check the "
                "broker's own platform and flatten by hand",
            )
            self._stand_down(now, "broker report not settling")

    def _working_orders(self, position: Position | None) -> dict[str, OrderStatus]:
        """Every order that may still be working, by id: the broker's record of what
        the system sent and what it lists (`TrackedBroker.working`), plus the
        position's own stop and exit (after a restart the record starts empty)."""
        working = self._broker.working()
        if position is not None:
            for order_id in (position.stop_order_id, position.exit_order_id):
                if order_id is None or order_id in working:
                    continue
                stand_in = Order(
                    order_id=order_id,
                    contract_id=position.contract_id,
                    side=Side.SELL,
                    quantity=position.contracts,
                    order_type=OrderType.LIMIT,
                    limit_price=self._p.instrument.option_tick_size,
                )
                status = self._broker.status_or_working(order_id, stand_in)
                if status.state is OrderState.WORKING:
                    working[order_id] = status
        return working

    def _reprice_stuck(
        self,
        reducing: list[OrderStatus],
        stuck_before: set[str],
        held: int,
        price: Decimal,
    ) -> None:
        """An order that would not cancel last round is moved to the flatten price
        instead (a stop is moved to trigger at once). Only orders whose sizes add up to
        no more than what is held: re-pricing more could sell short."""
        total = 0
        for status in reducing:
            remaining = status.order.quantity - status.filled
            if status.order.order_id not in stuck_before or total + remaining > held:
                continue
            total += remaining
            stop = price if status.order.order_type is OrderType.STOP_LIMIT else None
            try:
                self._broker.modify(status.order.order_id, price, stop)
            except Exception as exc:
                log.critical("could not re-price stuck %s: %r", status.order.order_id, exc)

    def _watch_stuck_cancels(self, pending: list[OrderStatus], now: datetime) -> None:
        if not pending:
            self._cancels_stuck_since, self._cancel_escalated = None, False
            self._alarmed_pending = ()
            return
        ids = tuple(sorted(status.order.order_id for status in pending))
        if ids != self._alarmed_pending:
            self._alarmed_pending = ids
            self._alarm(now, f"reconcile waiting for cancels to confirm: {list(ids)}")
        if self._cancels_stuck_since is None:
            self._cancels_stuck_since = now
        escalate = timedelta(seconds=self._p.execution.reconcile.cancel_escalate_seconds)
        if not self._cancel_escalated and now - self._cancels_stuck_since >= escalate:
            self._cancel_escalated = True
            self._alarm(
                now,
                f"CANCEL STUCK since {self._cancels_stuck_since.isoformat()}: {list(ids)}. "
                "Reconcile is re-pricing them to the market; if they do not fill, flatten "
                "by hand in the broker's own platform",
            )

    def _reconcile_price(self, contract_id: str, side: Side, now: datetime) -> Decimal | None:
        """The touch on a fresh quote; otherwise one tick past the last price per round
        (down for a sell, up for a buy), starting from the last known touch."""
        tick = self._p.instrument.option_tick_size
        max_age = timedelta(seconds=self._p.execution.quotes.max_age_seconds)
        quote = self._book.latest(contract_id)
        fresh = quote is not None and timedelta(0) <= now - quote.ts <= max_age
        touch = None
        if quote is not None:
            touch = quote.bid if side is Side.SELL else quote.ask
            if touch <= 0:
                touch = None
        last = self._reconcile_prices.get((contract_id, side))
        price: Decimal | None
        if fresh and touch is not None:
            price = touch
        elif last is not None:
            price = max(tick, last - tick) if side is Side.SELL else last + tick
        elif side is Side.SELL and quote is not None:
            price = touch if touch is not None else tick  # seen with no bid: one tick
        elif side is Side.SELL:
            price = None  # never seen a quote: no price to sell at (never one tick blind)
        else:
            price = touch  # a buy with no ask ever seen cannot be priced
        if price is not None:
            self._reconcile_prices[(contract_id, side)] = price
        return price

    def _reset_reconcile(self) -> None:
        self._acting = False
        self._unbooked_alarmed = False
        self._unrecorded_alarmed = set()
        self._unrecorded_named = set()
        self._unsettled_since, self._settle_escalated = None, False
        self._stuck_ids = set()
        self._seen.clear()
        self._reconcile_prices.clear()
        self._unpriced_alarmed = set()
        self._alarmed_mismatch = None
        self._alarmed_pending = ()
        self._cancels_stuck_since, self._cancel_escalated = None, False

    def _actual_exit(self, position: Position) -> tuple[Decimal, bool]:
        """The price of the one order that really closed THIS position (its stop, its
        exits, or reconcile orders sent for it), and whether one was found. Found only
        when a single sell, FILLED at a reported price, sold the whole position and no
        other sell of it sold anything: a position sold in parts (a stop that sold 1 of
        2 and was cancelled, then a reconcile sell of the rest), or sold twice, has no
        one exit price, and booking it at one of them is a guessed price (grind review
        G7-1). Then nothing is booked; Andre books it by hand (see `_sold_in_parts`)."""
        sold = self._sells_that_sold(position)
        if len(sold) == 1:
            (status,) = sold
            if (
                status.state is OrderState.FILLED
                and status.avg_price is not None
                and status.filled == position.contracts
            ):
                return status.avg_price, True
        return self._p.instrument.option_tick_size, False

    def _sold_in_parts(self, position: Position) -> str | None:
        """How this position was sold, when more than one sell, or a sell in part,
        sold it (so no single fill gives its exit price); None otherwise."""
        sold = self._sells_that_sold(position)
        if len(sold) == 1 and sold[0].filled == position.contracts:
            return None
        if not sold:
            return None
        return ", ".join(
            f"{s.order.order_id} sold {s.filled} of {s.order.quantity} "
            f"({s.state.value}, at {s.avg_price if s.avg_price is not None else 'no price yet'})"
            for s in sold
        )
