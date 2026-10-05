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
doubt (NB-16): on any mismatch it cancels every working order, flattens everything
the broker holds, latches the kill switch (only Andre restarts) and raises an alarm.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping
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
from footprint.execution.orders import (
    Order,
    OrderBroker,
    OrderState,
    OrderType,
    Quote,
    Side,
)

log = logging.getLogger("footprint.trade_mgmt")


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
        broker: OrderBroker,
        stops: StopKeeper,
        ledger: Ledger,
        halt: Halt,
        on_alarm: Callable[[Alarm], None] | None = None,
    ) -> None:
        self._p = profiles
        self._broker = broker
        self._stops = stops
        self._ledger = ledger
        self._halt = halt
        self._on_alarm = on_alarm or (lambda alarm: None)
        self._exit_reason: dict[str, ExitReason] = {}
        self._exit_attempts: dict[str, int] = {}
        self._reconcile_round = 0
        self._closed: set[str] = set()
        self._reconcile_orders: list[str] = []

    @property
    def agent_id(self) -> str:
        return "trade_mgmt"

    def manage(
        self,
        position: Position,
        quote: Quote,
        blackouts: Iterable[NewsBlackout],
        now: datetime,
    ) -> Closed | None:
        require_utc(now, "now")
        if quote.contract_id != position.contract_id:
            raise ValueError("quote is for a different contract than the position")
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
                # already filled, the trade is over; anything still open is cancelled and
                # confirmed first, so the exit can never be sold twice.
                with contextlib.suppress(KeyError):
                    orphan = self._broker.status(f"{pid}-stop")
                    if orphan.state is OrderState.FILLED and orphan.avg_price is not None:
                        return self._close(position, orphan.avg_price, now, ExitReason.STOP)
                for order in self._broker.open_orders():
                    if order.contract_id != position.contract_id:
                        continue
                    self._broker.cancel(order.order_id)
                    gone = self._broker.status(order.order_id)
                    if gone.state is OrderState.FILLED and gone.avg_price is not None:
                        return self._close(position, gone.avg_price, now, ExitReason.STOP)
                    if gone.state is OrderState.WORKING:
                        self._alarm(now, f"{pid}: {order.order_id} not cancelled yet; exit waits")
                        return None
        except Exception as exc:  # the broker failed mid-exit: halt, retry next call
            self._alarm(now, f"{pid}: broker error while exiting: {exc!r}; retrying")
            self._stand_down(now, f"{pid}: broker error while exiting")
            return None
        self._exit_reason[pid] = reason
        if reason in (ExitReason.GAP, ExitReason.UNPROTECTED, ExitReason.KILL_SWITCH):
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
        except Exception as exc:  # the exit did not reach the broker: retry next call
            self._alarm(now, f"{pid}: exit order failed to send: {exc!r}")
            position.exit_order_id = None
            self._stand_down(now, f"{pid}: exit failed to send")
            return None
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
        status = self._broker.status(position.exit_order_id)
        if status.state is OrderState.FILLED and status.avg_price is not None:
            return self._close(position, status.avg_price, now, reason)
        if status.state is not OrderState.WORKING:
            self._alarm(now, f"{position.position_id}: exit {status.state.value}; retrying")
            self._stand_down(now, f"{position.position_id}: exit {status.state.value}")
            return self._place_exit(position, quote, now, reason)
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

    def _alarm(self, now: datetime, message: str) -> None:
        log.critical(message)
        self._on_alarm(Alarm(now, message))

    def reconcile(
        self,
        position: Position | None,
        quotes: Mapping[str, Quote],
        now: datetime,
    ) -> bool:
        """True if the broker, the ledger and `position` all agree. On any doubt:
        cancel every order the BROKER lists, re-read what it still holds, post limit
        orders to flatten exactly that, latch the kill switch and alarm. Call again
        every few seconds until it returns True: each round re-reads the broker and
        re-prices the flatten, so it never sells what is no longer held.

        Booker's stops live in memory. After a restart, pass `position=None` (nothing
        the system can manage), so anything the broker still holds is flattened rather
        than run without its stop and trail.
        """
        expected = {position.contract_id: position.contracts} if position else {}
        expected_orders = {position.stop_order_id} if position and position.stop_order_id else set()
        ledger_open = self._ledger.open_position_id
        ledger_ok = ledger_open == (position.position_id if position else None)
        actual = self._broker.positions()
        open_ids = {o.order_id for o in self._broker.open_orders()}
        if actual == expected and ledger_ok and open_ids == expected_orders:
            return True
        self._alarm(
            now,
            f"reconcile mismatch: broker {actual} with orders {sorted(open_ids)}, expected "
            f"{expected} with {sorted(expected_orders)}, ledger open {ledger_open}",
        )
        self._halt.trip("unclear recovery: broker, ledger and records disagree")
        for order in self._broker.open_orders():
            self._broker.cancel(order.order_id)
        pending = [o.order_id for o in self._broker.open_orders()]
        if pending:
            # Cancels not confirmed yet: placing anything now could sell twice.
            self._alarm(now, f"reconcile waiting for cancels to confirm: {pending}")
            self._stand_down(now, "unclear recovery")
            return False
        remaining = self._broker.positions()  # after confirmed cancels: what is really held
        self._reconcile_round += 1
        tick = self._p.instrument.option_tick_size
        for contract_id, qty in remaining.items():
            quote = quotes.get(contract_id)
            if qty > 0:
                side, price = (
                    Side.SELL,
                    (quote.bid if quote is not None and quote.bid > 0 else tick),
                )
            elif quote is not None and quote.ask > 0:
                side, price = Side.BUY, quote.ask
            else:
                self._alarm(now, f"cannot price a buy-back for {contract_id}: no ask")
                continue
            order_id = f"reconcile-{self._reconcile_round}-{contract_id}"
            self._reconcile_orders.append(order_id)
            self._broker.place(
                Order(
                    order_id=order_id,
                    contract_id=contract_id,
                    side=side,
                    quantity=abs(qty),
                    order_type=OrderType.LIMIT,
                    limit_price=price,
                )
            )
        if not remaining and position is not None and ledger_open == position.position_id:
            price, actual_fill = self._actual_exit(position)
            if not actual_fill:
                self._alarm(now, f"{position.position_id}: booked at an ESTIMATED exit of {price}")
            self._close(position, price, now, ExitReason.RECONCILE)
        elif not remaining and ledger_open is not None and position is None:
            self._alarm(
                now,
                f"ledger shows {ledger_open} open but nothing is held and no record survives: "
                "book it with Ledger.book_by_hand",
            )
        self._stand_down(now, "unclear recovery: broker, ledger and records disagree")
        return False

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
