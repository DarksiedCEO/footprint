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
from zoneinfo import ZoneInfo

from footprint.core.calendar import SessionKind, session_at
from footprint.core.config import Profiles
from footprint.core.models import require_utc
from footprint.decision.session import NewsBlackout, in_news_blackout
from footprint.decision.stop import BidTick, StopCheck, StopKeeper
from footprint.execution.executor.diego import Position
from footprint.execution.halt import Halt
from footprint.execution.ledger import Ledger
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
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
        self._reconcile_round = 0
        self._closed: set[str] = set()
        self._reconcile_orders: list[str] = []
        self._seen: dict[str, tuple[object, datetime]] = {}
        self._reconcile_prices: dict[tuple[str, Side], Decimal] = {}
        self._alarmed_mismatch: object = None
        self._alarmed_pending: tuple[str, ...] = ()
        self._cancels_stuck_since: datetime | None = None
        self._cancel_escalated = False
        self._acting = False
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

        if position.stop_order_id is not None:
            stop_status = self._broker.status(position.stop_order_id)
            if stop_status.state is OrderState.FILLED and stop_status.avg_price is not None:
                return self._close(position, stop_status.avg_price, now, ExitReason.STOP)
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
            what = "no fresh quote" if check is StopCheck.STALE else "the bid has vanished"
            self._alarm(now, f"{pid}: {what}; relying on the resting stop")
            return None

        reason = self._must_exit(position, quote, check, blackouts, now)
        if reason is not None:
            return self._start_exit(position, quote, now, reason)

        self._stops.on_tick(pid, tick, now)
        self._sync_stop(position, now)
        return None

    def flatten(self, position: Position, now: datetime) -> Closed | None:
        """Get out using the broker and the quote book alone: no stop keeper, no ledger
        read, no news calendar. The runner calls this when normal management keeps
        failing. Call again each step until it returns the close."""
        require_utc(now, "now")
        if position.position_id in self._closed:
            raise RuntimeError(f"position {position.position_id!r} is already closed")
        quote = self._book.latest_or_empty(position.contract_id)
        if position.exit_order_id is not None:
            return self._work_exit(position, quote, now)
        return self._start_exit(position, quote, now, ExitReason.FAILSAFE)

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
        prime = p.instrument.prime_session
        zone = ZoneInfo(prime.timezone)
        local = now.astimezone(zone)
        cutoff = datetime.combine(local.date(), prime.end, zone) - timedelta(
            minutes=p.strategy.console.close_lead_minutes
        )
        return session_at(now, p.instrument) is not SessionKind.PRIME or local >= cutoff

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
            if position.stop_order_id is not None:
                self._broker.cancel(position.stop_order_id)
                cancelled = self._broker.status(position.stop_order_id)
                if cancelled.state is OrderState.FILLED and cancelled.avg_price is not None:
                    return self._close(position, cancelled.avg_price, now, ExitReason.STOP)
                if cancelled.state is not OrderState.CANCELLED:
                    self._alarm(now, f"{pid}: resting stop not cancelled ({cancelled.state.value})")
                    return None
                position.stop_order_id = None
            else:
                # Unprotected: a stop may have reached the broker unacknowledged. If it
                # already filled, the trade is over.
                with contextlib.suppress(KeyError):
                    orphan = self._broker.status(f"{pid}-stop")
                    if orphan.state is OrderState.FILLED and orphan.avg_price is not None:
                        return self._close(position, orphan.avg_price, now, ExitReason.STOP)
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
                        and gone.avg_price is not None
                        and gone.order.side is Side.SELL
                        and gone.filled == position.contracts
                    ):  # it sold exactly this position: the trade is over
                        return self._close(position, gone.avg_price, now, ExitReason.STOP)
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
                    self._alarm(now, f"{pid}: {order.order_id} not cancelled yet; exit waits")
                    return None
        except NeedsReconcile:
            raise
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
            self._alarm(now, f"{pid}: flattening ({reason.value})")
        return self._place_exit(position, quote, now, reason)

    def _place_exit(
        self, position: Position, quote: Quote, now: datetime, reason: ExitReason
    ) -> Closed | None:
        pid = position.position_id
        try:
            held = self._broker.positions().get(position.contract_id, 0)
        except Exception as exc:
            self._alarm(now, f"{pid}: cannot read positions before exiting: {exc!r}")
            self._stand_down(now, f"{pid}: cannot read positions")
            return None
        if 0 < held != position.contracts:
            # More (or fewer) held than this position: selling its size would leave
            # contracts nobody manages, or sell short. Stop and reconcile.
            message = f"{pid}: broker holds {held}, position is {position.contracts}; reconcile"
            self._alarm(now, message)
            self._stand_down(now, f"{pid}: held size differs from the position")
            raise NeedsReconcile(message)
        quantity = min(position.contracts, held)
        if quantity <= 0:
            # Nothing is held any more: something else closed it. Book the real fill.
            price, found = self._actual_exit(position)
            self._alarm(now, f"{pid}: already flat at the broker; booking {price}")
            if not found:
                self._stand_down(now, f"{pid}: closed by an unknown order")
            return self._close(position, price, now, reason)
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
        )
        position.exit_order_id = order.order_id
        position.events.append(f"{now.isoformat()} exit {reason.value} at {price}")
        try:
            status = self._broker.place(order)
        except Exception as exc:
            # The answer is lost, not necessarily the order: it may be working at the
            # broker. Keep its id (it is recorded and followed until confirmed
            # finished) and reconcile; never send another exit beside it blind.
            message = f"{pid}: exit {order.order_id} sent but the answer was lost: {exc!r}"
            self._alarm(now, message)
            self._stand_down(now, f"{pid}: exit outcome unknown")
            raise NeedsReconcile(message) from exc
        if status.state is OrderState.FILLED and status.avg_price is not None:
            return self._close(position, status.avg_price, now, reason)
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
        except NeedsReconcile:
            raise
        except Exception as exc:  # broker error: halt, keep the exit recorded, retry
            self._alarm(now, f"{position.position_id}: broker error working the exit: {exc!r}")
            self._stand_down(now, f"{position.position_id}: broker error working the exit")
            return None

    def _work_exit_once(
        self, position: Position, quote: Quote, now: datetime, reason: ExitReason
    ) -> Closed | None:
        assert position.exit_order_id is not None
        status = self._broker.status(position.exit_order_id)
        if status.state is OrderState.FILLED and status.avg_price is not None:
            return self._close(position, status.avg_price, now, reason)
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
            if status.state is OrderState.FILLED and status.avg_price is not None:
                return self._close(position, status.avg_price, now, reason)
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
            self._ledger.on_trade_closed(now, position.position_id, net)
        else:
            self._alarm(now, f"{position.position_id}: closed but the ledger never recorded it")
        with contextlib.suppress(KeyError):  # Booker never held a stop (unprotected case)
            self._stops.close(position.position_id)
        self._closed.add(position.position_id)
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
        actual = self._broker.positions()
        working = self._working_orders(position)
        working_ids = set(working)
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
                self._broker.cancel(order_id)
            except Exception as exc:  # chased again next round
                log.critical("reconcile could not cancel %s: %r", order_id, exc)
        pending = list(self._working_orders(position).values())
        self._watch_stuck_cancels(pending, now)
        remaining = self._broker.positions()  # what is really held after the cancels
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
            side = Side.SELL if qty > 0 else Side.BUY
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
                self._alarm(now, f"cannot price a buy-back for {contract_id}: no ask")
                continue
            self._reprice_stuck(reducing, stuck_before, abs(qty), price)
            if needed <= 0:
                continue  # orders that would not cancel already cover it
            order = Order(
                order_id=f"reconcile-{self._reconcile_round}-{contract_id}",
                contract_id=contract_id,
                side=side,
                quantity=needed,
                order_type=OrderType.LIMIT,
                limit_price=price,
            )
            self._reconcile_orders.append(order.order_id)
            self._broker.place(order)  # recorded and followed by id from now on
        if not remaining and position is not None and ledger_open == position.position_id:
            price, actual_fill = self._actual_exit(position)
            if not actual_fill:
                self._alarm(now, f"{position.position_id}: booked at an ESTIMATED exit of {price}")
            self._reconcile_closes.append(self._close(position, price, now, ExitReason.RECONCILE))
        elif not remaining and ledger_open is not None and position is None:
            self._alarm(
                now,
                f"ledger shows {ledger_open} open but nothing is held and no record survives: "
                "book it with Ledger.book_by_hand",
            )
        self._stand_down(now, "unclear recovery: broker, ledger and records disagree")
        return False

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
        elif side is Side.SELL:
            price = touch if touch is not None else tick
        else:
            price = touch  # a buy with no ask ever seen cannot be priced
        if price is not None:
            self._reconcile_prices[(contract_id, side)] = price
        return price

    def _reset_reconcile(self) -> None:
        self._acting = False
        self._unsettled_since, self._settle_escalated = None, False
        self._stuck_ids = set()
        self._seen.clear()
        self._reconcile_prices.clear()
        self._alarmed_mismatch = None
        self._alarmed_pending = ()
        self._cancels_stuck_since, self._cancel_escalated = None, False

    def _actual_exit(self, position: Position) -> tuple[Decimal, bool]:
        """The price of the order that really closed the position, or an estimate."""
        candidates = [position.stop_order_id, f"{position.position_id}-stop"]
        candidates += [
            f"{position.position_id}-exit-{n}"
            for n in range(self._exit_attempts.get(position.position_id, 0), 0, -1)
        ]
        candidates += list(reversed(self._reconcile_orders))
        for order_id in candidates:
            if order_id is None:
                continue
            try:
                status = self._broker.status(order_id)
            except KeyError:
                continue
            if (
                status.state is OrderState.FILLED
                and status.avg_price is not None
                and status.order.side is Side.SELL
                and status.order.contract_id == position.contract_id
            ):
                return status.avg_price, True
        return self._p.instrument.option_tick_size, False
