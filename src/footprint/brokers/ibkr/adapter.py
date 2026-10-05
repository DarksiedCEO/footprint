"""IbkrBroker: our OrderBroker, over Interactive Brokers.

IBKR is asynchronous; the system's broker seam is not. Each call sends, then waits
(up to `ack_timeout`) for IBKR's answer about that order. What the adapter adds,
each for a failure IBKR is known to have:

- Nothing is sent while the connection is down, or until a full resync (open
  orders of every client, executions, positions) has completed after any connect
  or reconnect. A modify sent while IBKR's connectivity is degraded can come back as
  a SECOND live order under the same order id (a new permId) while the first stays
  live; so every call fails fast (BrokerUnavailable) instead, and the system's
  reconcile, with its escalation to Andre, takes over.
- Every live copy of an order is tracked by its permId. Two live copies of one
  order are alarmed, both count as working (so a flatten covers them), and
  cancelling the extra copy uses a broker-wide cancel (IBKR cancels by order id,
  which both copies share).
- Our order id travels in IBKR's orderRef, so after a restart every open order is
  matched back to ours; orders with no orderRef (placed by hand) are listed as
  foreign, so reconcile sees them.
- Positions are counted from executions (deduplicated by execId) on top of the
  snapshot taken at the last resync, and checked against IBKR's own position
  stream. Where the two disagree, only what both agree on is reported, so nothing
  can be sold that may already be gone; a disagreement that lasts is alarmed.
- A resend under the same order id is safe at IBKR (an open order is modified in
  place, not duplicated); IBKR's answers that the order already exists (103 duplicate
  id, 104 cannot modify a filled order, 105 does not match the original) raise
  DuplicateOrderId, as the order record (TrackedBroker) requires.
- A position's stop and exits go out as one one-cancels-all group, type 1 (cancel
  the rest, with block): IBKR itself lets only one of them fill.
- The system sees only its own instrument (`scope`: symbol and security type, e.g.
  the configured futures options). Positions, orders and fills in anything else in the account
  (a stock Andre holds, an order he placed by hand on something else) are invisible
  to it, so reconcile can never sell or cancel them; and the broker-wide cancel is
  refused while any such order is open (Andre is asked to cancel by hand instead).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from footprint.brokers.ibkr.gateway import (
    Connection,
    IbContract,
    IbGateway,
    IbOrderRequest,
)
from footprint.core.config import IbkrConfig
from footprint.execution.orders import (
    DuplicateOrderId,
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
)

log = logging.getLogger("footprint.ibkr")

_LIVE = frozenset({"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"})
_CANCELLED = frozenset({"Cancelled", "ApiCancelled"})
_FILLED = "Filled"
_CANNOT_MODIFY_FILLED = 104
_EXISTS = frozenset({103, _CANNOT_MODIFY_FILLED, 105})  # the order is already at IBKR
_REJECTED = frozenset({110, 201})
_CANCEL_SETTLED = frozenset({135, 161, 202})  # not found / not cancellable / cancelled
_LOST, _RESTORED_DATA_LOST, _RESTORED = 1100, 1101, 1102
_OCA_CANCEL_WITH_BLOCK = 1
_EXTRA = "#"  # an extra live copy of an order is listed as "<order id>#<permId>"


class BrokerUnavailable(ConnectionError):
    """Disconnected, or not yet resynced: nothing is sent and nothing is trusted."""


@dataclass(frozen=True, slots=True)
class IbkrSettings:
    ack_timeout: timedelta
    positions_disagree_alarm: timedelta

    @classmethod
    def from_config(cls, ibkr: IbkrConfig) -> IbkrSettings:
        return cls(
            ack_timeout=timedelta(seconds=ibkr.ack_timeout_seconds),
            positions_disagree_alarm=timedelta(seconds=ibkr.positions_disagree_alarm_seconds),
        )


class ContractBook:
    """Our contract ids <-> IBKR contracts (filled from IBKR's contract details)."""

    def __init__(self) -> None:
        self._by_id: dict[str, IbContract] = {}
        self._by_con: dict[int, str] = {}

    def register(self, contract_id: str, contract: IbContract) -> None:
        known = self._by_id.get(contract_id)
        if known is not None and known.con_id != contract.con_id:
            raise ValueError(f"{contract_id} is already {known.con_id}, not {contract.con_id}")
        self._by_id[contract_id] = contract
        self._by_con[contract.con_id] = contract_id

    def ib(self, contract_id: str) -> IbContract:
        return self._by_id[contract_id]

    def contract_id(self, con_id: int) -> str:
        """Ours, or a stand-in for a contract never registered (a hand-placed order)."""
        return self._by_con.get(con_id, f"conid:{con_id}")


@dataclass
class _Copy:
    """One live (or finished) incarnation of an order at IBKR, by permId."""

    perm_id: int
    ib_id: int
    order_id: str
    order: Order
    status: str
    filled: int = 0
    avg_price: Decimal | None = None

    @property
    def live(self) -> bool:
        return self.status in _LIVE

    @property
    def state(self) -> OrderState:
        if self.live:
            return OrderState.WORKING
        if self.status == _FILLED:
            return OrderState.FILLED
        if self.status in _CANCELLED:
            return OrderState.CANCELLED
        return OrderState.REJECTED  # Inactive, or anything unrecognised and not live


class IbkrBroker:
    def __init__(
        self,
        gateway: IbGateway,
        contracts: ContractBook,
        settings: IbkrSettings,
        scope: tuple[str, str],
        on_alarm: Callable[[str], None] = lambda message: None,
        on_resync: Callable[[], None] = lambda: None,
    ) -> None:
        self._gw = gateway
        self._contracts = contracts
        self._scope = scope
        self._outside_orders: set[int] = set()  # live orders on other instruments (permId)
        self._timeout = settings.ack_timeout.total_seconds()
        self._disagree_limit = settings.positions_disagree_alarm.total_seconds()
        self._on_alarm = on_alarm
        self._on_resync = on_resync
        self._cv = threading.Condition()
        self._connected = False
        self._ready = False
        self._awaiting: set[str] = set()
        self._next_id: int | None = None
        self._ids: dict[str, int] = {}
        self._order_of_ib: dict[int, str] = {}
        self._sent: dict[str, Order] = {}
        self._copies: dict[int, _Copy] = {}
        self._updates: dict[int, int] = {}
        self._errors: dict[int, list[int]] = {}
        self._exec_seen: set[str] = set()
        self._base: dict[str, int] = {}
        self._delta: dict[str, int] = {}
        self._snapshot: dict[str, int] = {}
        self._ib_positions: dict[str, int] = {}
        self._disagree_since: float | None = None
        self._disagree_alarmed = False
        self._duplicates_alarmed: set[int] = set()

    # --- OrderBroker ------------------------------------------------------------------

    def place(self, order: Order) -> OrderStatus:
        contract = self._contracts.ib(order.contract_id)
        with self._cv:
            self._require_ready()
            ib_id = self._ids.get(order.order_id)
            if ib_id is None:
                ib_id = self._allocate(order.order_id)
            self._sent[order.order_id] = order
            self._errors[ib_id] = []
            seen = self._updates.get(ib_id, 0)
        self._gw.place_order(self._request(ib_id, order, contract))
        with self._cv:
            self._wait(lambda: self._answered(ib_id, seen), f"placing {order.order_id}")
            codes = self._errors.get(ib_id, [])
            if _EXISTS & set(codes):
                raise DuplicateOrderId(f"{order.order_id} is already at IBKR ({codes})")
            if _REJECTED & set(codes) and not self._copies_of(order.order_id):
                return OrderStatus(order, OrderState.REJECTED, 0, None)
            unknown = [c for c in codes if c not in _REJECTED]
            if unknown and not self._copies_of(order.order_id):
                raise RuntimeError(f"IBKR error {unknown} placing {order.order_id}")
            return self._status(order.order_id)

    def modify(
        self, order_id: str, limit_price: Decimal, stop_price: Decimal | None
    ) -> OrderStatus:
        with self._cv:
            self._require_ready()
            ib_id = self._ids[order_id]
            current = self._status(order_id)
            if current.state is not OrderState.WORKING:
                return current
            order = replace(current.order, limit_price=limit_price, stop_price=stop_price)
            contract = self._contracts.ib(order.contract_id)
            self._errors[ib_id] = []
            seen = self._updates.get(ib_id, 0)
        self._gw.place_order(self._request(ib_id, order, contract))
        with self._cv:
            self._wait(lambda: self._answered(ib_id, seen), f"modifying {order_id}")
            codes = self._errors.get(ib_id, [])
            if _CANNOT_MODIFY_FILLED in codes:  # filled before the modify arrived
                return self._status(order_id)
            if codes:
                raise RuntimeError(f"IBKR error {codes} modifying {order_id}")
            self._sent[order_id] = order
            return self._status(order_id)

    def cancel(self, order_id: str) -> OrderStatus:
        with self._cv:
            self._require_ready()
            ib_id = self._ids.get(order_id)
            extra = _EXTRA in order_id or ib_id is None or self._live_copies(order_id) > 1
            if not extra:
                assert ib_id is not None
                self._errors[ib_id] = []
        if extra:
            # Extra copies share their order id at IBKR, and a hand-placed order may
            # not be ours to cancel by id: only a broker-wide cancel reaches them. It
            # would also cancel orders on anything else in the account, so it is not
            # sent while any are open: Andre cancels by hand instead.
            with self._cv:
                outside = len(self._outside_orders)
            if outside:
                self._alarm(
                    f"CANCEL BY HAND: {order_id} needs a broker-wide cancel, but "
                    f"{outside} order(s) on other instruments are open"
                )
                with self._cv:
                    return self._status(order_id)
            self._alarm(f"broker-wide cancel to reach {order_id}")
            self._gw.global_cancel()
            with self._cv:
                self._cv.wait_for(lambda: not self._is_live(order_id), self._timeout)
                return self._status(order_id)
        assert ib_id is not None
        self._gw.cancel_order(ib_id)
        with self._cv:
            self._cv.wait_for(
                lambda: (
                    not self._connected
                    or not self._is_live(order_id)
                    or bool(_CANCEL_SETTLED & set(self._errors.get(ib_id, [])))
                ),
                self._timeout,
            )
            self._require_ready()
            return self._status(order_id)

    def status(self, order_id: str) -> OrderStatus:
        with self._cv:
            self._require_ready()
            return self._status(order_id)

    def positions(self) -> dict[str, int]:
        """What both the executions and IBKR's position report agree is held."""
        with self._cv:
            self._require_ready()
            counted = self._counted()
            reported = {k: v for k, v in self._ib_positions.items() if v}
            agreed: dict[str, int] = {}
            for contract_id in set(counted) | set(reported):
                a, b = counted.get(contract_id, 0), reported.get(contract_id, 0)
                if a == b or (a > 0 and b > 0) or (a < 0 and b < 0):
                    held = a if a == b else (min(a, b) if a > 0 else max(a, b))
                    if held:
                        agreed[contract_id] = held
            disagree = counted != reported
            alarm = self._track_disagreement(disagree, counted, reported)
        if alarm is not None:
            self._alarm(alarm)
        return agreed

    def open_orders(self) -> list[Order]:
        with self._cv:
            self._require_ready()
            listed: list[Order] = []
            for order_id in {c.order_id for c in self._copies.values() if c.live}:
                live = sorted(
                    (c for c in self._copies.values() if c.order_id == order_id and c.live),
                    key=lambda c: c.perm_id,
                )
                listed.append(self._order_for(live[0], order_id))
                listed += [
                    replace(self._order_for(c, order_id), order_id=f"{order_id}{_EXTRA}{c.perm_id}")
                    for c in live[1:]
                ]
            return listed

    # --- IbEvents: called by the gateway binding, on its own thread --------------------

    def on_connection(self, state: Connection, next_valid_id: int | None = None) -> None:
        with self._cv:
            if state is Connection.LOST:
                self._connected = self._ready = False
                self._cv.notify_all()
                lost = True
            else:
                lost = False
                self._connected = True
                if next_valid_id is not None:
                    self._next_id = max(next_valid_id, self._next_id or 0)
                self._begin_resync()
        if lost:
            self._alarm("IBKR connection lost: nothing is sent until it is back and resynced")
            return
        self._gw.request_open_orders()
        self._gw.request_executions()
        self._gw.request_positions()

    def on_open_order(
        self,
        ib_id: int,
        perm_id: int,
        order_ref: str,
        con_id: int,
        symbol: str,
        sec_type: str,
        action: str,
        quantity: int,
        order_type: str,
        limit_price: Decimal,
        stop_price: Decimal | None,
        oca_group: str | None,
        status: str,
    ) -> None:
        alarms: list[str] = []
        with self._cv:
            if (symbol, sec_type) != self._scope:
                if status in _LIVE:
                    self._outside_orders.add(perm_id)
                else:
                    self._outside_orders.discard(perm_id)
                return
            order_id = order_ref or self._order_of_ib.get(ib_id) or f"ib-{perm_id}"
            if order_ref:
                self._ids.setdefault(order_ref, ib_id)
                self._order_of_ib[ib_id] = order_ref
            order = self._rebuild(
                order_id, con_id, action, quantity, order_type, limit_price, stop_price, oca_group
            )
            copy = self._copies.get(perm_id)
            if copy is None:
                copy = self._copies[perm_id] = _Copy(perm_id, ib_id, order_id, order, status)
            else:
                copy.order, copy.status = order, status
            alarms += self._check_duplicates(order_id)
            self._bump(ib_id)
        for message in alarms:
            self._alarm(message)

    def on_open_orders_end(self) -> None:
        self._end("orders")

    def on_order_status(
        self,
        ib_id: int,
        perm_id: int,
        status: str,
        filled: int,
        remaining: int,
        avg_fill_price: Decimal,
    ) -> None:
        alarms: list[str] = []
        with self._cv:
            if perm_id in self._outside_orders:
                if status not in _LIVE:
                    self._outside_orders.discard(perm_id)
                return
            copy = self._copies.get(perm_id)
            if copy is None:
                order_id = self._order_of_ib.get(ib_id, f"ib-{perm_id}")
                order = self._sent.get(order_id)
                if order is None:
                    self._bump(ib_id)
                    return  # an order we know nothing of yet: its openOrder will follow
                copy = self._copies[perm_id] = _Copy(perm_id, ib_id, order_id, order, status)
            copy.status, copy.filled = status, filled
            copy.avg_price = avg_fill_price if filled else None
            alarms += self._check_duplicates(copy.order_id)
            self._bump(ib_id)
        for message in alarms:
            self._alarm(message)

    def on_execution(
        self,
        exec_id: str,
        ib_id: int,
        perm_id: int,
        con_id: int,
        symbol: str,
        sec_type: str,
        side: str,
        shares: int,
        price: Decimal,
    ) -> None:
        if (symbol, sec_type) != self._scope:
            return
        with self._cv:
            if exec_id in self._exec_seen:
                return
            self._exec_seen.add(exec_id)
            if self._awaiting:
                return  # resyncing: already inside the position snapshot being taken
            contract_id = self._contracts.contract_id(con_id)
            signed = shares if side == "BOT" else -shares
            self._delta[contract_id] = self._delta.get(contract_id, 0) + signed
            self._cv.notify_all()

    def on_executions_end(self) -> None:
        self._end("executions")

    def on_position(self, con_id: int, symbol: str, sec_type: str, position: int) -> None:
        if (symbol, sec_type) != self._scope:
            return
        with self._cv:
            contract_id = self._contracts.contract_id(con_id)
            if "positions" in self._awaiting:
                self._snapshot[contract_id] = position
            else:
                self._ib_positions[contract_id] = position
            self._cv.notify_all()

    def on_positions_end(self) -> None:
        with self._cv:
            self._base = {k: v for k, v in self._snapshot.items() if v}
            self._ib_positions = dict(self._base)
            self._delta = {}
        self._end("positions")

    def on_error(self, req_id: int, code: int, message: str) -> None:
        if code == _LOST:
            self.on_connection(Connection.LOST)
            return
        if code == _RESTORED_DATA_LOST:
            self.on_connection(Connection.RESTORED_DATA_LOST)
            return
        if code == _RESTORED:
            self.on_connection(Connection.RESTORED)
            return
        if req_id <= 0:
            log.info("IBKR notice %s: %s", code, message)
            return
        with self._cv:
            self._errors.setdefault(req_id, []).append(code)
            self._bump(req_id)
        log.warning("IBKR error %s on %s: %s", code, req_id, message)

    # --- internals (callers hold the lock unless noted) --------------------------------

    def _require_ready(self) -> None:
        if not (self._connected and self._ready):
            raise BrokerUnavailable("IBKR is disconnected or not yet resynced")

    def _allocate(self, order_id: str) -> int:
        if self._next_id is None:
            raise BrokerUnavailable("no order id from IBKR yet")
        ib_id = self._next_id
        self._next_id += 1
        self._ids[order_id] = ib_id
        self._order_of_ib[ib_id] = order_id
        return ib_id

    def _request(self, ib_id: int, order: Order, contract: IbContract) -> IbOrderRequest:
        return IbOrderRequest(
            ib_id=ib_id,
            contract=contract,
            action="BUY" if order.side is Side.BUY else "SELL",
            quantity=order.quantity,
            order_type="STP LMT" if order.order_type is OrderType.STOP_LIMIT else "LMT",
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            order_ref=order.order_id,
            oca_group=order.oca_group,
            oca_type=_OCA_CANCEL_WITH_BLOCK if order.oca_group is not None else None,
            tif="DAY",
        )

    def _answered(self, ib_id: int, seen: int) -> bool:
        return not self._connected or self._updates.get(ib_id, 0) > seen

    def _wait(self, predicate: Callable[[], bool], what: str) -> None:
        if not self._cv.wait_for(predicate, self._timeout):
            raise TimeoutError(f"no answer from IBKR {what}")
        if not self._connected:
            raise BrokerUnavailable(f"IBKR disconnected while {what}")

    def _bump(self, ib_id: int) -> None:
        self._updates[ib_id] = self._updates.get(ib_id, 0) + 1
        self._cv.notify_all()

    def _copies_of(self, order_id: str) -> list[_Copy]:
        return [c for c in self._copies.values() if c.order_id == order_id]

    def _live_copies(self, order_id: str) -> int:
        return sum(1 for c in self._copies_of(order_id) if c.live)

    def _is_live(self, order_id: str) -> bool:
        if _EXTRA in order_id:
            copy = self._copies.get(int(order_id.rsplit(_EXTRA, 1)[1]))
            return copy is not None and copy.live
        return self._live_copies(order_id) > 0

    def _status(self, order_id: str) -> OrderStatus:
        if _EXTRA in order_id:
            base, perm = order_id.rsplit(_EXTRA, 1)
            copy = self._copies[int(perm)]
            order = replace(self._order_for(copy, base), order_id=order_id)
            return OrderStatus(order, copy.state, copy.filled, copy.avg_price)
        copies = self._copies_of(order_id)
        if not copies:
            raise KeyError(order_id)  # IBKR has not acknowledged it (yet)
        order = self._sent.get(order_id) or copies[0].order
        live = [c for c in copies if c.live]
        if live:
            order = live[0].order if order_id not in self._sent else self._sent[order_id]
        filled = sum(c.filled for c in copies)
        priced = [c for c in copies if c.filled and c.avg_price is not None]
        avg = (
            sum((c.avg_price * c.filled for c in priced if c.avg_price is not None), Decimal(0))
            / sum(c.filled for c in priced)
            if priced
            else None
        )
        if live:
            state = OrderState.WORKING
        elif any(c.status == _FILLED for c in copies):
            state = OrderState.FILLED
        elif all(c.status in _CANCELLED for c in copies):
            state = OrderState.CANCELLED
        else:
            state = OrderState.REJECTED
        return OrderStatus(order, state, filled, avg)

    def _order_for(self, copy: _Copy, order_id: str) -> Order:
        return self._sent.get(order_id, copy.order)

    def _rebuild(
        self,
        order_id: str,
        con_id: int,
        action: str,
        quantity: int,
        order_type: str,
        limit_price: Decimal,
        stop_price: Decimal | None,
        oca_group: str | None,
    ) -> Order:
        contract_id = self._contracts.contract_id(con_id)
        side = Side.BUY if action == "BUY" else Side.SELL
        stand_in = Decimal(1)  # a hand-placed order may carry no limit (a market order)
        limit = limit_price if limit_price > 0 else stand_in
        try:
            if order_type == "STP LMT" and stop_price is not None:
                return Order(
                    order_id,
                    contract_id,
                    side,
                    quantity,
                    OrderType.STOP_LIMIT,
                    limit,
                    stop_price,
                    oca_group,
                )
            return Order(
                order_id, contract_id, side, quantity, OrderType.LIMIT, limit, None, oca_group
            )
        except (TypeError, ValueError):  # unusual fields on a hand-placed order
            return Order(
                order_id,
                contract_id,
                side,
                max(1, quantity),
                OrderType.LIMIT,
                stand_in,
                None,
                oca_group,
            )

    def _check_duplicates(self, order_id: str) -> list[str]:
        live = [c for c in self._copies_of(order_id) if c.live]
        if len(live) <= 1:
            return []
        fresh = [c for c in live if c.perm_id not in self._duplicates_alarmed]
        self._duplicates_alarmed |= {c.perm_id for c in live}
        if not fresh:
            return []
        perms = sorted(c.perm_id for c in live)
        return [f"DUPLICATE LIVE ORDER at IBKR: {order_id} has copies {perms}; reconcile"]

    def _counted(self) -> dict[str, int]:
        counted = dict(self._base)
        for contract_id, delta in self._delta.items():
            counted[contract_id] = counted.get(contract_id, 0) + delta
        return {k: v for k, v in counted.items() if v}

    def _track_disagreement(
        self, disagree: bool, counted: dict[str, int], reported: dict[str, int]
    ) -> str | None:
        if not disagree:
            self._disagree_since, self._disagree_alarmed = None, False
            return None
        now = time.monotonic()
        if self._disagree_since is None:
            self._disagree_since = now
        if not self._disagree_alarmed and now - self._disagree_since >= self._disagree_limit:
            self._disagree_alarmed = True
            return (
                f"IBKR positions {reported} disagree with the fills {counted}; "
                "only what both agree on is acted on"
            )
        return None

    def _begin_resync(self) -> None:
        self._ready = False
        self._outside_orders = set()  # relearned from the open orders
        self._awaiting = {"orders", "executions", "positions"}
        self._snapshot = {}

    def _end(self, part: str) -> None:
        resynced = False
        with self._cv:
            self._awaiting.discard(part)
            if not self._awaiting and self._connected and not self._ready:
                self._ready = resynced = True
            self._cv.notify_all()
        if resynced:
            log.info("IBKR resynced")
            self._on_resync()

    def _alarm(self, message: str) -> None:
        log.critical(message)
        try:
            self._on_alarm(message)
        except Exception:
            log.exception("could not deliver an IBKR alarm")
