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
        on_alarm: Callable[[Alarm], None] | None = None,
    ) -> None:
        self._p = profiles
        self._broker = broker
        self._stops = stops
        self._ledger = ledger
        self._on_alarm = on_alarm or (lambda alarm: None)
        self._exit_reason: dict[str, ExitReason] = {}
        self._exit_attempts: dict[str, int] = {}
        self._reconcile_round = 0

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
        if check is StopCheck.STALE:
            self._alarm(now, f"{pid}: no fresh quote; relying on the resting stop")
            return None
        if check is StopCheck.NO_BID:
            self._alarm(now, f"{pid}: the bid has vanished; resting stop stays, watch closely")
            return None

        reason = self._must_exit(position, quote, check, blackouts, now)
        if reason is not None:
            return self._start_exit(position, quote, now, reason)

        before = self._stops.current(pid).price
        self._stops.on_tick(pid, tick, now)
        after = self._stops.current(pid)
        if after.price > before and position.stop_order_id is not None:
            spec = self._stops.resting_order(pid)
            self._broker.modify(position.stop_order_id, spec.limit, spec.trigger)
            confirmed = self._broker.status(position.stop_order_id)
            if confirmed.order.stop_price != spec.trigger and confirmed.state is OrderState.WORKING:
                self._alarm(
                    now,
                    f"{pid}: broker did not raise the resting stop to {spec.trigger} "
                    f"(still {confirmed.order.stop_price})",
                )
            position.events.append(f"{now.isoformat()} stop raised to {spec.trigger}")
        return None

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
        prime = p.instrument.prime_session
        zone = ZoneInfo(prime.timezone)
        local = now.astimezone(zone)
        cutoff = datetime.combine(local.date(), prime.end, zone) - timedelta(
            minutes=p.strategy.console.close_lead_minutes
        )
        if session_at(now, p.instrument) is not SessionKind.PRIME or local >= cutoff:
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
        if position.stop_order_id is not None:
            self._broker.cancel(position.stop_order_id)
            cancelled = self._broker.status(position.stop_order_id)
            if cancelled.state is OrderState.FILLED and cancelled.avg_price is not None:
                return self._close(position, cancelled.avg_price, now, ExitReason.STOP)
            if cancelled.state is not OrderState.CANCELLED:
                self._alarm(now, f"{pid}: resting stop not cancelled ({cancelled.state.value})")
                return None
            position.stop_order_id = None
        self._exit_reason[pid] = reason
        if reason in (ExitReason.GAP, ExitReason.UNPROTECTED, ExitReason.KILL_SWITCH):
            self._alarm(now, f"{pid}: flattening ({reason.value})")
        return self._place_exit(position, quote, now, reason)

    def _place_exit(
        self, position: Position, quote: Quote, now: datetime, reason: ExitReason
    ) -> Closed | None:
        pid = position.position_id
        self._exit_attempts[pid] = self._exit_attempts.get(pid, 0) + 1
        tick = self._p.instrument.option_tick_size
        # A sell limit at the bid; with no bid, at one tick (the floor of any sale).
        price = quote.bid if quote.bid > 0 else tick
        order = Order(
            order_id=f"{pid}-exit-{self._exit_attempts[pid]}",
            contract_id=position.contract_id,
            side=Side.SELL,
            quantity=position.contracts,
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
            self._ledger.latch_kill_switch(f"{pid}: exit failed to send")
            return None
        if status.state is OrderState.FILLED and status.avg_price is not None:
            return self._close(position, status.avg_price, now, reason)
        if status.state is not OrderState.WORKING:
            self._alarm(now, f"{pid}: exit {status.state.value}; retrying and halting entries")
            position.exit_order_id = None
            self._ledger.latch_kill_switch(f"{pid}: exit {status.state.value}")
        return None

    def _work_exit(self, position: Position, quote: Quote, now: datetime) -> Closed | None:
        assert position.exit_order_id is not None
        reason = self._exit_reason.get(position.position_id, ExitReason.STOP)
        status = self._broker.status(position.exit_order_id)
        if status.state is OrderState.FILLED and status.avg_price is not None:
            return self._close(position, status.avg_price, now, reason)
        if status.state is not OrderState.WORKING:
            self._alarm(now, f"{position.position_id}: exit {status.state.value}; retrying")
            self._ledger.latch_kill_switch(f"{position.position_id}: exit {status.state.value}")
            return self._place_exit(position, quote, now, reason)
        target = quote.bid if quote.bid > 0 else self._p.instrument.option_tick_size
        if target < status.order.limit_price:
            self._broker.modify(position.exit_order_id, target, None)
            status = self._broker.status(position.exit_order_id)
            if status.state is OrderState.FILLED and status.avg_price is not None:
                return self._close(position, status.avg_price, now, reason)
        return None

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
        ledger_open = self._ledger.open_position_id
        ledger_ok = ledger_open == (position.position_id if position else None)
        actual = self._broker.positions()
        if actual == expected and ledger_ok:
            return True
        self._alarm(
            now,
            f"reconcile mismatch: broker {actual}, expected {expected}, ledger open {ledger_open}",
        )
        self._ledger.latch_kill_switch("unclear recovery: broker, ledger and records disagree")
        for order in self._broker.open_orders():
            self._broker.cancel(order.order_id)
        remaining = self._broker.positions()  # after the cancels: what is really held
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
            self._broker.place(
                Order(
                    order_id=f"reconcile-{self._reconcile_round}-{contract_id}",
                    contract_id=contract_id,
                    side=side,
                    quantity=abs(qty),
                    order_type=OrderType.LIMIT,
                    limit_price=price,
                )
            )
        if not remaining and position is not None and ledger_open == position.position_id:
            quote = quotes.get(position.contract_id)
            estimate = quote.bid if quote is not None and quote.bid > 0 else tick
            self._alarm(now, f"{position.position_id}: booked at an ESTIMATED exit of {estimate}")
            self._close(position, estimate, now, ExitReason.RECONCILE)
        elif not remaining and ledger_open is not None and position is None:
            self._alarm(
                now,
                f"ledger shows {ledger_open} open but nothing is held and no record survives: "
                "book it by hand",
            )
        return False
