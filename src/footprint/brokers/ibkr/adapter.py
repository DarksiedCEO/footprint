"""IbkrBroker: our OrderBroker, over Interactive Brokers.

IBKR is asynchronous; the system's broker seam is not. Each call sends, then waits
(up to `ack_timeout`) for IBKR's answer about that order. What the adapter adds,
each for a failure IBKR is known to have:

- Nothing is sent while the connection is down, or until a full resync has
  completed after any connect or reconnect: open orders of every client and the
  day's executions, THEN a position snapshot. A modify sent while IBKR's
  connectivity is degraded can come back as a SECOND live order under the same order
  id (a new permId) while the first stays live; so every call fails fast
  (BrokerUnavailable) instead, and the system's reconcile takes over.
- The position snapshot is taken only after the execution replay has ended, so
  every replayed fill is inside it. A fill that arrives WHILE the snapshot is being
  taken may or may not be inside it, so that snapshot is thrown away and taken
  again. Fills after the resync are counted on top of the snapshot.
- Positions are counted from executions and checked against IBKR's own position
  stream. They are reported only when the two agree; while they disagree (a report
  lagging a fill, or a miscount) `positions()` raises PositionsInDoubt, so nothing
  is sold or booked on a number that may be wrong. A disagreement that lasts
  `positions_disagree_alarm` is alarmed and a fresh resync is started.
- Executions are keyed by execId without its correction suffix: a correction
  replaces the fill it corrects instead of adding to it.
- Every live copy of an order is tracked by its permId. Two live copies of one
  order are alarmed, both count as working (so a flatten covers them), and
  cancelling the extra copy uses a broker-wide cancel (IBKR cancels by order id,
  which both copies share).
- An order of ours that IBKR no longer lists as open after a resync finished while
  the system could not see it: it is settled from the executions (FILLED if they
  fill it, otherwise CANCELLED) and alarmed. A cancel IBKR answers with "not found"
  or "cannot cancel" is checked the same way, against a fresh list of open orders.
- "Inactive" is not trusted as finished (an inactive order can become active
  again): it counts as working, it is cancelled, and it is finished only once IBKR
  confirms the cancel.
- A modify is confirmed only by IBKR echoing the new prices on a live copy (an
  unrelated status already on its way is not an answer). What is reported for an
  order is always what IBKR says it holds, never what was asked for.
- IBKR's warnings (TIF set from a preset, order message warnings, farm notices) are
  logged, never taken as an order's answer or as an error.
- Our order id travels in IBKR's orderRef, so after a restart every open order is
  matched back to ours; orders with no orderRef (placed by hand) are listed as
  foreign, so reconcile sees them.
- A resend under the same order id is safe at IBKR (an open order is modified in
  place, not duplicated); IBKR's answers that the order already exists (103 duplicate
  id, 104 cannot modify a filled order, 105 does not match the original) raise
  DuplicateOrderId, as the order record (TrackedBroker) requires.
- A position's stop and exits go out as one one-cancels-all group, type 1 (cancel
  the rest, with block): IBKR itself lets only one of them fill.
- The system sees only its own instrument (`scope`: symbol and security type, e.g.
  the configured futures options). Positions, orders and fills in anything else in
  the account (a stock Andre holds, an order he placed by hand on something else)
  are invisible to it, so reconcile can never sell or cancel them; and the
  broker-wide cancel is refused while any such order is open (Andre is asked to
  cancel by hand instead). An option of the system's own instrument held by hand
  IS in scope: reconcile will try to flatten it and fail on the unregistered
  contract, halting the system. Do not trade the instrument by hand in this account.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    PositionsUncertain,
    Side,
)

log = logging.getLogger("footprint.ibkr")

_LIVE = frozenset({"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"})
_CANCELLED = frozenset({"Cancelled", "ApiCancelled"})
_FILLED = "Filled"
_INACTIVE = "Inactive"
_CANCELLED_STATUS = "Cancelled"
_CANNOT_MODIFY_FILLED = 104
_EXISTS = frozenset({103, _CANNOT_MODIFY_FILLED, 105})  # the order is already at IBKR
_REJECTED = frozenset({110, 201})
_CANCEL_SETTLED = frozenset({135, 161, 202})  # not found / not cancellable / cancelled
_LOST, _RESTORED_DATA_LOST, _RESTORED = 1100, 1101, 1102
# Warnings and notices, not answers: farm status (2100-2199), order message warning,
# delayed data, TIF set from a preset (the order stays live).
_WARNINGS = frozenset({165, 399, 10167, 10349})
_NOTICES = range(2100, 2200)
_OCA_CANCEL_WITH_BLOCK = 1
_EXTRA = "#"  # an extra live copy of an order is listed as "<order id>#<permId>"


class BrokerUnavailable(ConnectionError):
    """Disconnected, or not yet resynced: nothing is sent and nothing is trusted."""


class PositionsInDoubt(BrokerUnavailable, PositionsUncertain):
    """The fills and IBKR's position report disagree: no position can be trusted."""


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
    order: Order  # as IBKR holds it
    status: str
    filled: int = 0
    avg_price: Decimal | None = None
    cancel_sent: bool = False  # an Inactive copy the adapter asked IBKR to cancel
    cancel_confirmed: bool = False  # ...and IBKR answered that cancel
    settled_unseen: bool = False  # taken as finished because IBKR stopped listing it

    @property
    def live(self) -> bool:
        if self.status == _INACTIVE:
            return not self.cancel_confirmed
        return self.status in _LIVE

    @property
    def state(self) -> OrderState:
        if self.live:
            return OrderState.WORKING
        if self.status == _FILLED:
            return OrderState.FILLED
        if self.status in _CANCELLED:
            return OrderState.CANCELLED
        return OrderState.REJECTED  # an Inactive copy confirmed cancelled, or unrecognised


@dataclass
class _Fill:
    exec_id: str  # the latest version (corrections bump the suffix)
    contract_id: str
    perm_id: int
    signed: int
    price: Decimal


@dataclass
class _Listing:
    """One request for every open order: which of ours were live when it was asked."""

    expected: set[int]
    resync: bool
    listed: set[int] = field(default_factory=set)
    done: bool = False


def _exec_key(exec_id: str) -> tuple[str, str]:
    """(the fill, its version): IBKR corrects a fill under a new last segment."""
    base, dot, version = exec_id.rpartition(".")
    return (base, version) if dot else (exec_id, "")


def _later(version: str, than: str) -> bool:
    if version.isdigit() and than.isdigit():
        return int(version) > int(than)
    return version > than


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
        self._doubt_limit = settings.positions_disagree_alarm.total_seconds()
        self._on_alarm = on_alarm
        self._on_resync = on_resync
        self._cv = threading.Condition()
        self._connected = False
        self._ready = False
        self._awaiting: set[str] = set()
        self._positions_round = False  # the snapshot has been asked for, not yet ended
        self._round_dirty = False  # a fill arrived while the snapshot was being taken
        self._resync_since: float | None = None
        self._resync_alarmed = False
        self._listing: _Listing | None = None
        self._unlisted_at_resync: set[int] = set()
        # Requests asked and not yet ended, by kind: only the end of the LAST one asked
        # completes a listing, a replay or a snapshot (IBKR's ends carry no request id).
        self._outstanding: dict[str, int] = {}
        self._next_id: int | None = None
        self._ids: dict[str, int] = {}
        self._order_of_ib: dict[int, str] = {}
        self._sent: dict[str, Order] = {}
        self._copies: dict[int, _Copy] = {}
        self._updates: dict[int, int] = {}
        self._errors: dict[int, list[int]] = {}
        self._fills: dict[str, _Fill] = {}
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
            # Rejected with a copy at IBKR (Inactive): it reports working until its
            # cancel is confirmed.
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
        self._gw.place_order(self._request(ib_id, order, contract))
        with self._cv:
            # Only IBKR echoing the new prices confirms a modify; a status already on
            # its way is not its answer.
            self._wait(
                lambda: self._modified(order_id, ib_id, limit_price, stop_price),
                f"modifying {order_id}",
            )
            codes = self._errors.get(ib_id, [])
            if _CANNOT_MODIFY_FILLED in codes:  # filled before the modify arrived
                return self._status(order_id)
            if codes:
                raise RuntimeError(f"IBKR error {codes} modifying {order_id}")
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
        with self._cv:
            for copy in self._copies_of(order_id):
                if copy.status == _INACTIVE:
                    copy.cancel_sent = True
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
            # "Not found" / "cannot cancel" while we still think it live: IBKR's own
            # list of open orders decides.
            unsure = self._is_live(order_id) and bool(
                _CANCEL_SETTLED & set(self._errors.get(ib_id, []))
            )
        if unsure:
            self._audit(order_id)
        with self._cv:
            self._require_ready()
            return self._status(order_id)

    def status(self, order_id: str) -> OrderStatus:
        with self._cv:
            self._require_ready()
            return self._status(order_id)

    def positions(self) -> dict[str, int]:
        """What the executions and IBKR's position report agree is held. A report a
        moment behind a fill is waited for (up to `ack_timeout`); while they still
        disagree nothing is reported: PositionsInDoubt."""
        resync = False
        alarm: str | None = None
        with self._cv:
            self._require_ready()
            self._cv.wait_for(
                lambda: not self._ready or self._counted() == self._reported(), self._timeout
            )
            self._require_ready()
            counted = self._counted()
            reported = self._reported()
            if counted == reported:
                self._disagree_since, self._disagree_alarmed = None, False
                return counted
            now = time.monotonic()
            if self._disagree_since is None:
                self._disagree_since = now
            if not self._disagree_alarmed and now - self._disagree_since >= self._doubt_limit:
                self._disagree_alarmed = resync = True
                alarm = (
                    f"IBKR positions {reported} disagree with the fills {counted}: "
                    "nothing is acted on; resyncing"
                )
                self._begin_resync()
        if alarm is not None:
            self._alarm(alarm)
        if resync:
            self._request_resync()
        raise PositionsInDoubt(f"IBKR positions {reported} disagree with the fills {counted}")

    def open_orders(self) -> list[Order]:
        with self._cv:
            self._require_ready()
            listed: list[Order] = []
            for order_id in {c.order_id for c in self._copies.values() if c.live}:
                live = sorted(
                    (c for c in self._copies.values() if c.order_id == order_id and c.live),
                    key=lambda c: c.perm_id,
                )
                listed.append(live[0].order)
                listed += [
                    replace(c.order, order_id=f"{order_id}{_EXTRA}{c.perm_id}") for c in live[1:]
                ]
            return listed

    @property
    def ready(self) -> bool:
        """Connected and resynced: calls will be answered."""
        with self._cv:
            return self._connected and self._ready

    @property
    def outside_open_orders(self) -> int:
        """Live orders on anything outside the system's scope (seen at the last resync)."""
        with self._cv:
            return len(self._outside_orders)

    # --- IbEvents: called by the gateway binding, on its own thread --------------------

    def on_connection(self, state: Connection, next_valid_id: int | None = None) -> None:
        with self._cv:
            if state is Connection.LOST:
                self._connected = self._ready = False
                self._cv.notify_all()
            else:
                self._connected = True
                if state is Connection.CONNECTED:
                    self._outstanding = {}  # a new session: no old request can answer
                if next_valid_id is not None:
                    self._next_id = max(next_valid_id, self._next_id or 0)
                self._begin_resync()
        if state is Connection.LOST:
            self._alarm("IBKR connection lost: nothing is sent until it is back and resynced")
            return
        self._request_resync()

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
        back: list[str] = []
        cancel: int | None = None
        with self._cv:
            if (symbol, sec_type) != self._scope:
                if status in _LIVE or status == _INACTIVE:
                    self._outside_orders.add(perm_id)
                else:
                    self._outside_orders.discard(perm_id)
                return
            if self._listing is not None:
                self._listing.listed.add(perm_id)
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
            back = self._came_back(copy)
            alarms += back
            alarms += self._check_duplicates(order_id)
            cancel = self._inactive(copy, alarms)
            self._bump(ib_id)
        self._after_event(alarms, cancel, doubt=bool(back))

    def on_open_orders_end(self) -> None:
        resynced = False
        alarms: list[str] = []
        with self._cv:
            listing = self._listing
            if not self._last_end("orders") or listing is None:
                return
            self._listing = None
            listing.done = True
            unlisted = listing.expected - listing.listed
            if listing.resync:
                self._unlisted_at_resync = unlisted
                self._awaiting.discard("orders")
                resynced = self._maybe_ready(alarms)
            else:
                alarms += self._settle_unlisted(unlisted)
            self._cv.notify_all()
        self._after_event(alarms, None)
        if resynced:
            self._resynced()

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
        back: list[str] = []
        cancel: int | None = None
        with self._cv:
            if perm_id in self._outside_orders:
                if status not in _LIVE and status != _INACTIVE:
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
            copy.status = status
            back = self._came_back(copy)
            alarms += back
            copy.filled = max(filled, self._exec_filled(perm_id))
            copy.avg_price = avg_fill_price if filled else self._exec_price(perm_id)
            alarms += self._check_duplicates(copy.order_id)
            cancel = self._inactive(copy, alarms)
            self._bump(ib_id)
        self._after_event(alarms, cancel, doubt=bool(back))

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
        key, version = _exec_key(exec_id)
        signed = shares if side == "BOT" else -shares
        with self._cv:
            known = self._fills.get(key)
            if known is not None:
                if known.exec_id == exec_id or not _later(version, _exec_key(known.exec_id)[1]):
                    return  # told again, or an older version of a corrected fill
                change = signed - known.signed  # a correction replaces the fill
                known.exec_id, known.signed, known.price = exec_id, signed, price
                contract_id = known.contract_id
            else:
                contract_id = self._contracts.contract_id(con_id)
                self._fills[key] = _Fill(exec_id, contract_id, perm_id, signed, price)
                change = signed
            if self._awaiting:
                # Before the snapshot is asked for, the snapshot will include it. While
                # it is being taken, it may or may not: that snapshot is taken again.
                if self._positions_round:
                    self._round_dirty = True
            elif change:
                self._delta[contract_id] = self._delta.get(contract_id, 0) + change
            copy = self._copies.get(perm_id)
            if copy is not None:
                copy.filled = max(copy.filled, self._exec_filled(perm_id))
            self._cv.notify_all()

    def on_executions_end(self) -> None:
        with self._cv:
            if not self._last_end("executions") or "executions" not in self._awaiting:
                return
            self._awaiting.discard("executions")
            # Every replayed fill is in: only now can a snapshot be known to hold them.
            self._positions_round, self._round_dirty, self._snapshot = True, False, {}
            self._ask("positions")
        self._gw.request_positions()

    def on_position(self, con_id: int, symbol: str, sec_type: str, position: int) -> None:
        if (symbol, sec_type) != self._scope:
            return
        with self._cv:
            contract_id = self._contracts.contract_id(con_id)
            if self._positions_round:
                self._snapshot[contract_id] = position
            else:
                self._ib_positions[contract_id] = position
            self._cv.notify_all()

    def on_positions_end(self) -> None:
        resynced, again = False, False
        alarms: list[str] = []
        with self._cv:
            if not self._last_end("positions") or not self._positions_round:
                return
            if self._round_dirty:
                self._round_dirty, self._snapshot = False, {}
                self._ask("positions")
                again = True
            else:
                self._positions_round = False
                self._base = {k: v for k, v in self._snapshot.items() if v}
                self._ib_positions = dict(self._base)
                self._delta = {}
                self._disagree_since, self._disagree_alarmed = None, False
                self._awaiting.discard("positions")
                resynced = self._maybe_ready(alarms)
        if again:
            log.warning("IBKR: a fill arrived during the position snapshot; taking it again")
            self._gw.request_positions()
        self._after_event(alarms, None)
        if resynced:
            self._resynced()

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
        if req_id <= 0 or code in _WARNINGS or code in _NOTICES:
            log.info("IBKR notice %s on %s: %s", code, req_id, message)
            return
        with self._cv:
            self._errors.setdefault(req_id, []).append(code)
            if code in _CANCEL_SETTLED:
                for copy in self._copies.values():
                    if copy.ib_id == req_id and copy.status == _INACTIVE and copy.cancel_sent:
                        copy.cancel_confirmed = True  # IBKR answered the cancel
            self._bump(req_id)
        log.warning("IBKR error %s on %s: %s", code, req_id, message)

    # --- internals (callers hold the lock unless noted) --------------------------------

    def _require_ready(self) -> None:
        if self._connected and self._ready:
            return
        if (
            self._connected
            and self._resync_since is not None
            and not self._resync_alarmed
            and time.monotonic() - self._resync_since >= self._doubt_limit
        ):
            self._resync_alarmed = True
            self._alarm("IBKR resync is not completing: nothing is sent or trusted")
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

    def _modified(
        self, order_id: str, ib_id: int, limit_price: Decimal, stop_price: Decimal | None
    ) -> bool:
        if not self._connected or self._errors.get(ib_id):
            return True
        live = [c for c in self._copies_of(order_id) if c.live]
        if not live:
            return True  # finished meanwhile: its status says how
        return any(
            c.order.limit_price == limit_price and c.order.stop_price == stop_price for c in live
        )

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
            copy = self._copies[int(order_id.rsplit(_EXTRA, 1)[1])]
            order = replace(copy.order, order_id=order_id)
            return OrderStatus(order, copy.state, copy.filled, copy.avg_price)
        copies = sorted(self._copies_of(order_id), key=lambda c: c.perm_id)
        if not copies:
            raise KeyError(order_id)  # IBKR has not acknowledged it (yet)
        live = [c for c in copies if c.live]
        order = (live or copies)[0].order  # as IBKR holds it, never as asked
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

    def _exec_filled(self, perm_id: int) -> int:
        return sum(abs(f.signed) for f in self._fills.values() if f.perm_id == perm_id)

    def _exec_price(self, perm_id: int) -> Decimal | None:
        fills = [f for f in self._fills.values() if f.perm_id == perm_id and f.signed]
        if not fills:
            return None
        return sum((f.price * abs(f.signed) for f in fills), Decimal(0)) / sum(
            abs(f.signed) for f in fills
        )

    def _inactive(self, copy: _Copy, alarms: list[str]) -> int | None:
        """An Inactive order of ours is cancelled (it could become active again); the
        id to cancel, sent by the caller once the lock is released."""
        if copy.status != _INACTIVE or copy.cancel_sent:
            return None
        if copy.ib_id <= 0 or self._ids.get(copy.order_id) != copy.ib_id:
            return None  # not ours to cancel by id
        copy.cancel_sent = True
        alarms.append(f"{copy.order_id} is Inactive at IBKR: cancelling it")
        return copy.ib_id

    def _came_back(self, copy: _Copy) -> list[str]:
        """An order taken as finished unseen that IBKR now reports live again."""
        if not copy.settled_unseen or copy.status not in _LIVE:
            return []
        copy.settled_unseen = copy.cancel_confirmed = False
        return [f"{copy.order_id} is LIVE again at IBKR after it was taken as finished; reconcile"]

    def _after_event(self, alarms: list[str], cancel: int | None, *, doubt: bool = False) -> None:
        """Outside the lock: the alarms an event raised, a cancel it needs, and the
        runner sent to reconcile when what IBKR holds is not what we believed."""
        for message in alarms:
            self._alarm(message)
        if cancel is not None:
            self._gw.cancel_order(cancel)
        if doubt:
            self._on_resync()

    def _settle_unlisted(self, perms: set[int]) -> list[str]:
        """Our copies IBKR no longer lists as open finished unseen: by the fills."""
        alarms: list[str] = []
        for perm in sorted(perms):
            copy = self._copies.get(perm)
            if copy is None or not copy.live:
                continue  # its status arrived meanwhile
            copy.filled = max(copy.filled, self._exec_filled(perm))
            copy.avg_price = self._exec_price(perm) or copy.avg_price
            copy.status = _FILLED if copy.filled >= copy.order.quantity else _CANCELLED_STATUS
            copy.cancel_confirmed = copy.settled_unseen = True
            if copy.status == _FILLED:
                alarms.append(f"{copy.order_id} filled while unseen ({copy.filled} filled)")
            else:
                alarms.append(
                    f"{copy.order_id} is no longer open at IBKR and no status said why: it "
                    f"finished unseen, taken as cancelled ({copy.filled} filled). CHECK TWS: "
                    "if it is still working there, cancel it by hand"
                )
            self._bump(copy.ib_id)
        return alarms

    def _audit(self, order_id: str) -> None:
        """Ask IBKR for every open order, and settle ours it no longer lists. Called
        without the lock."""
        with self._cv:
            self._cv.wait_for(lambda: self._listing is None, self._timeout)
            if self._listing is not None or not self._ready:
                return
            listing = _Listing({c.perm_id for c in self._copies.values() if c.live}, False)
            self._listing = listing
            self._ask("orders")
        log.warning("IBKR: checking the open orders for %s", order_id)
        self._gw.request_open_orders()
        with self._cv:
            self._cv.wait_for(lambda: listing.done or not self._connected, self._timeout)
            if self._listing is listing:
                self._listing = None  # no answer: the next audit asks again

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

    def _reported(self) -> dict[str, int]:
        return {k: v for k, v in self._ib_positions.items() if v}

    def _begin_resync(self) -> None:
        self._ready = False
        self._outside_orders = set()  # relearned from the open orders
        self._awaiting = {"orders", "executions", "positions"}
        self._positions_round, self._round_dirty, self._snapshot = False, False, {}
        self._unlisted_at_resync = set()
        self._listing = _Listing({c.perm_id for c in self._copies.values() if c.live}, True)
        self._resync_since, self._resync_alarmed = time.monotonic(), False
        self._ask("orders")
        self._ask("executions")
        self._cv.notify_all()

    def _ask(self, kind: str) -> None:
        self._outstanding[kind] = self._outstanding.get(kind, 0) + 1

    def _last_end(self, kind: str) -> bool:
        """An end has arrived: True if it ends the last request asked of this kind."""
        left = max(0, self._outstanding.get(kind, 0) - 1)
        self._outstanding[kind] = left
        return left == 0

    def _request_resync(self) -> None:
        """Without the lock: orders and executions first; positions after them."""
        self._gw.request_open_orders()
        self._gw.request_executions()

    def _maybe_ready(self, alarms: list[str]) -> bool:
        if self._awaiting or not self._connected or self._ready:
            return False
        alarms += self._settle_unlisted(self._unlisted_at_resync)
        self._unlisted_at_resync = set()
        self._ready = True
        self._resync_since = None
        self._cv.notify_all()
        return True

    def _resynced(self) -> None:
        log.info("IBKR resynced")
        self._on_resync()

    def _alarm(self, message: str) -> None:
        log.critical(message)
        try:
            self._on_alarm(message)
        except Exception:
            log.exception("could not deliver an IBKR alarm")
