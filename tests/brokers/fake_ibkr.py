"""A fake IBKR for the adapter's tests.

Its matching engine is the project's SimulatedBroker (fills only against the shared
quote book); on top, it speaks IBKR: integer order ids and permIds, orderRef,
openOrder / orderStatus / execDetails / position events, IBKR's error codes, and the
failures IBKR is known for, each switched on by a test:

- lose_answers: the next sends reach the exchange, but the answers to THOSE sends
  are held back (later messages, and the answer to a resend, still arrive);
- drop_sends: the send never arrives;
- degraded: a modify creates a SECOND live copy under the same order id (new permId)
  instead of changing the first (seen at IBKR during connectivity loss);
- position_lag: IBKR's position report holds back changes until flushed;
- lose() / restore(): the connection drops and comes back (1100, 1101, 1102);
- during_snapshot: run between the position snapshot and its end (a fill then);
- during_replay: run in the middle of the execution replay;
- slow_cancels: a cancel answers PendingCancel; the order stays live (and can
  fill) until finish_cancels();
- cancel_answer: the next cancel is answered only with this error code;
- mute_cancels: a cancel is never answered;
- modify_error: the next modify is refused with this code; a stale status arrives
  first, the refusal only on release();
- warn_on_send: every send is first answered with this warning code;
- inactivate() / reactivate(): IBKR reports an order Inactive, then live again;
- correct(): IBKR corrects a fill under a new execId suffix;
- status_first: a new order's status (and fill) arrives before its openOrder;
- mute_listing: a request for the open orders is never answered.

Every modify is echoed by an openOrder with the new prices, as IBKR does. The open
orders replay lists only open orders, and a status changed while disconnected is
told only if a test lets it be.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker
from footprint.brokers.ibkr.gateway import Connection, IbOrderRequest
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)

D0 = Decimal(0)
_STATUS = {
    OrderState.WORKING: "Submitted",
    OrderState.FILLED: "Filled",
    OrderState.CANCELLED: "Cancelled",
    OrderState.REJECTED: "Inactive",
}


@dataclass
class _Copy:
    perm_id: int
    ib_id: int
    request: IbOrderRequest
    sim_id: str
    sent: tuple[OrderState, int] | None = None  # last state/filled told to the adapter


@dataclass
class FakeIbkr:
    book: QuoteBook
    contracts: ContractBook
    max_age_seconds: int = 5
    sim: SimulatedBroker = field(init=False)
    events: IbkrBroker | None = None
    connected: bool = False
    degraded: bool = False
    lose_answers: int = 0  # the next N sends: done, but their events held back
    drop_sends: int = 0  # the next N sends: never arrive
    position_lag: bool = False
    reject_next: int | None = None  # the next send is answered with this error code
    mute_reports: bool = False  # status/fill/position changes are not told (until unmuted)
    defer_executions: bool = False  # the execution replay waits for flush_executions()
    during_snapshot: Callable[[], None] | None = None
    during_replay: Callable[[], None] | None = None
    slow_cancels: bool = False
    pending_cancels: list[int] = field(default_factory=list)  # permIds
    cancel_answer: int | None = None
    mute_cancels: bool = False  # a cancel is never answered
    modify_error: int | None = None
    warn_on_send: int | None = None
    inactive: set[int] = field(default_factory=set)  # permIds reported Inactive
    position_requests: int = 0
    status_first: bool = False  # a new order's status is told before its openOrder
    mute_listing: bool = False  # a request for the open orders is never answered
    copies: dict[int, _Copy] = field(default_factory=dict)
    held: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    executions: list[tuple[str, int, int, int, str, str, str, int, Decimal]] = field(
        default_factory=list
    )
    told_positions: dict[int, int] = field(default_factory=dict)
    sends: list[IbOrderRequest] = field(default_factory=list)
    untransmitted: dict[int, IbOrderRequest] = field(default_factory=dict)  # transmit off
    cancels: list[int] = field(default_factory=list)
    global_cancels: int = 0
    _perm: int = 1000
    _hold_now: bool = False

    def __post_init__(self) -> None:
        self.sim = SimulatedBroker(self.book, self.max_age_seconds)
        self.book.subscribe(lambda quote: self._report())  # after the sim has filled

    # --- the test's levers ------------------------------------------------------------

    def attach(self, adapter: IbkrBroker) -> None:
        self.events = adapter

    def connect(self, next_valid_id: int = 1) -> None:
        self.connected = True
        assert self.events is not None
        self.events.on_connection(Connection.CONNECTED, next_valid_id)

    def lose(self) -> None:
        self.connected = False
        assert self.events is not None
        self.events.on_error(-1, 1100, "Connectivity between IB and TWS has been lost.")

    def restore(self, *, data_lost: bool) -> None:
        self.connected = True
        assert self.events is not None
        code = 1101 if data_lost else 1102
        self.events.on_error(-1, code, "Connectivity between IB and TWS has been restored")

    def release(self) -> None:
        """Deliver every event that was held back (the answers that were 'lost')."""
        held, self.held = self.held, []
        for name, args in held:
            self._deliver(name, args)

    def flush_executions(self) -> None:
        self.defer_executions = False
        self.request_executions()

    def flush_positions(self) -> None:
        self.position_lag = False
        self._report()

    # --- IbGateway -----------------------------------------------------------------

    def finish_cancels(self) -> None:
        pending, self.pending_cancels = self.pending_cancels, []
        for perm in pending:
            copy = self.copies[perm]
            if self.sim.status(copy.sim_id).state is OrderState.WORKING:
                self.sim.cancel(copy.sim_id)
        self._report()

    def inactivate(self, perm: int) -> None:
        self.inactive.add(perm)
        self._report(force=self.copies[perm].ib_id)

    def reactivate(self, perm: int) -> None:
        self.inactive.discard(perm)
        self._open_order(self.copies[perm], self.sim.status(self.copies[perm].sim_id))
        self._report(force=self.copies[perm].ib_id)

    def correct(self, index: int, shares: int) -> None:
        """IBKR corrects execution `index`: same fill, a new version, new shares."""
        old = self.executions[index]
        base, _, version = old[0].rpartition(".")
        fixed = (f"{base}.{int(version) + 1:02d}", *old[1:7], shares, old[8])
        self.executions[index] = fixed
        self._emit("on_execution", fixed)

    def place_order(self, request: IbOrderRequest) -> None:
        self.sends.append(request)
        if not self.connected:
            return
        if self.warn_on_send is not None:
            self._emit("on_error", (request.ib_id, self.warn_on_send, "warning"))
        if self.drop_sends:
            self.drop_sends -= 1
            return
        if self.lose_answers:
            self.lose_answers -= 1
            self._hold_now = True
        try:
            self._place(request)
        finally:
            self._hold_now = False

    def _place(self, request: IbOrderRequest) -> None:
        if not request.transmit:  # IBKR keeps it, unsent and silent, until transmitted
            self.untransmitted[request.ib_id] = request
            return
        if request.parent_ib_id is not None:
            parent = self.untransmitted.pop(request.parent_ib_id)
            self._new_bracket(parent, request)
            return
        if self.reject_next is not None:
            code, self.reject_next = self.reject_next, None
            self._emit("on_error", (request.ib_id, code, "rejected"), request.ib_id)
            return
        mine = [c for c in self.copies.values() if c.ib_id == request.ib_id]
        live = [c for c in mine if self.sim.status(c.sim_id).state is OrderState.WORKING]
        if live and not self.degraded:
            if self.modify_error is not None:
                code, self.modify_error = self.modify_error, None
                self._report(force=request.ib_id)  # a stale status, already on its way
                self.held.append(("on_error", (request.ib_id, code, "modify refused")))
                return  # the refusal itself comes later: release()
            for copy in live:  # an in-place modify, echoed with the new prices
                copy.request = request
                self.sim.modify(copy.sim_id, request.limit_price, request.stop_price)
                self._open_order(copy, self.sim.status(copy.sim_id))
            self._report(force=request.ib_id)
            return
        if mine and not live:
            done = any(self.sim.status(c.sim_id).state is OrderState.FILLED for c in mine)
            code = 104 if done else 103
            self._emit("on_error", (request.ib_id, code, "exists"), request.ib_id)
            return
        self._new_copy(request)  # a new order (or, degraded, a second live copy)

    def cancel_order(self, ib_id: int) -> None:
        self.cancels.append(ib_id)
        if self.mute_cancels:
            return
        if self.cancel_answer is not None:
            code, self.cancel_answer = self.cancel_answer, None
            self._emit("on_error", (ib_id, code, "cancel answer"), ib_id)
            return
        mine = [c for c in self.copies.values() if c.ib_id == ib_id]
        live = [c for c in mine if self.sim.status(c.sim_id).state is OrderState.WORKING]
        if not live:
            filled = any(self.sim.status(c.sim_id).state is OrderState.FILLED for c in mine)
            if filled:
                self._emit("on_error", (ib_id, 161, "Cancel attempted when filled"), ib_id)
            else:
                self._emit("on_error", (ib_id, 135, "Can't find order"), ib_id)
            return
        copy = live[0]  # IBKR reaches one copy per order id
        self.inactive.discard(copy.perm_id)
        if self.slow_cancels:
            self.pending_cancels.append(copy.perm_id)
            self._emit(
                "on_order_status",
                (copy.ib_id, copy.perm_id, "PendingCancel", 0, copy.request.quantity, D0),
                ib_id,
            )
            return
        self.sim.cancel(copy.sim_id)
        self._report()

    def global_cancel(self) -> None:
        self.global_cancels += 1
        for copy in self.copies.values():
            if self.sim.status(copy.sim_id).state is OrderState.WORKING:
                self.sim.cancel(copy.sim_id)
        self._report()

    def request_open_orders(self) -> None:
        if self.mute_listing:
            return
        for copy in self.copies.values():
            status = self.sim.status(copy.sim_id)
            if status.state is OrderState.WORKING:
                self._open_order(copy, status)
        self._emit("on_open_orders_end", ())

    def request_executions(self) -> None:
        if self.defer_executions:
            return
        replay = list(self.executions)
        for n, execution in enumerate(replay):
            self._emit("on_execution", execution)
            if n == 0 and self.during_replay is not None:
                hook, self.during_replay = self.during_replay, None
                hook()
        self._emit("on_executions_end", ())

    def request_positions(self) -> None:
        self.position_requests += 1
        for con_id, position in self._positions().items():
            self._emit("on_position", (con_id, *self._kind(con_id), position))
            self.told_positions[con_id] = position
        if self.during_snapshot is not None:
            hook, self.during_snapshot = self.during_snapshot, None
            hook()
        self._emit("on_positions_end", ())

    # --- the exchange -----------------------------------------------------------------

    def _sim_order(self, request: IbOrderRequest) -> _Copy:
        self._perm += 1
        sim_id = f"{request.order_ref}@{self._perm}"
        copy = _Copy(self._perm, request.ib_id, request, sim_id)
        self.copies[copy.perm_id] = copy
        return copy

    def _order(self, copy: _Copy, parent_sim: str | None = None) -> Order:
        request = copy.request
        stop = request.order_type == "STP LMT"
        return Order(
            copy.sim_id,
            self.contracts.contract_id(request.contract.con_id),
            Side.BUY if request.action == "BUY" else Side.SELL,
            request.quantity,
            OrderType.STOP_LIMIT if stop else OrderType.LIMIT,
            request.limit_price,
            request.stop_price if stop else None,
            request.oca_group,
            parent_sim,
        )

    def _new_bracket(self, parent: IbOrderRequest, child: IbOrderRequest) -> None:
        """Both transmitted together; the child held until the parent fills."""
        if self.reject_next is not None:  # the child is refused (and the parent with it)
            code, self.reject_next = self.reject_next, None
            for request in (parent, child):
                self._emit("on_error", (request.ib_id, code, "rejected"), request.ib_id)
            return
        first = self._sim_order(parent)
        second = self._sim_order(child)
        self.sim.place_attached(self._order(first), self._order(second, first.sim_id))
        for copy in (first, second):
            self._open_order(copy, self.sim.status(copy.sim_id))
        self._report(force=parent.ib_id)
        self._report(force=child.ib_id)

    def _new_copy(self, request: IbOrderRequest) -> None:
        copy = self._sim_order(request)
        sim_id = copy.sim_id
        self.sim.place(self._order(copy))
        if self.status_first:
            self._report(force=request.ib_id)
            self._open_order(copy, self.sim.status(sim_id))
        else:
            self._open_order(copy, self.sim.status(sim_id))
            self._report(force=request.ib_id)

    def _open_order(self, copy: _Copy, status: OrderStatus) -> None:
        r = copy.request
        self._emit(
            "on_open_order",
            (
                copy.ib_id,
                copy.perm_id,
                r.order_ref,
                r.contract.con_id,
                r.contract.symbol,
                r.contract.sec_type,
                r.action,
                r.quantity,
                r.order_type,
                r.limit_price,
                r.stop_price,
                r.oca_group,
                self._said(copy, status),
            ),
            copy.ib_id,
        )

    def _said(self, copy: _Copy, status: OrderStatus) -> str:
        if copy.perm_id in self.inactive and status.state is OrderState.WORKING:
            return "Inactive"
        if copy.perm_id in self.pending_cancels and status.state is OrderState.WORKING:
            return "PendingCancel"
        return _STATUS[status.state]

    def _report(self, force: int | None = None) -> None:
        """Tell the adapter what changed: statuses, executions, positions."""
        if self.mute_reports:
            return
        for copy in self.copies.values():
            status = self.sim.status(copy.sim_id)
            now = (status.state, status.filled)
            if now == copy.sent and copy.ib_id != force:
                continue
            new_fill = status.filled - (copy.sent[1] if copy.sent else 0)
            copy.sent = now
            if new_fill > 0 and status.avg_price is not None:
                execution = (
                    f"0000e{len(self.executions) + 1:04x}.65a1b2c3.01.01",
                    copy.ib_id,
                    copy.perm_id,
                    copy.request.contract.con_id,
                    copy.request.contract.symbol,
                    copy.request.contract.sec_type,
                    "BOT" if copy.request.action == "BUY" else "SLD",
                    new_fill,
                    status.avg_price,
                )
                self.executions.append(execution)
                self._emit("on_execution", execution, copy.ib_id)
            self._emit(
                "on_order_status",
                (
                    copy.ib_id,
                    copy.perm_id,
                    self._said(copy, status),
                    status.filled,
                    copy.request.quantity - status.filled,
                    status.avg_price or Decimal(0),
                ),
                copy.ib_id,
            )
        if not self.position_lag:
            for con_id, position in {
                **{k: 0 for k in self.told_positions},
                **self._positions(),
            }.items():
                if self.told_positions.get(con_id) != position:
                    self.told_positions[con_id] = position
                    self._emit("on_position", (con_id, *self._kind(con_id), position))

    def _kind(self, con_id: int) -> tuple[str, str]:
        c = self.contracts.ib(self.contracts.contract_id(con_id))
        return c.symbol, c.sec_type

    def _positions(self) -> dict[int, int]:
        return {self.contracts.ib(cid).con_id: qty for cid, qty in self.sim.positions().items()}

    def _emit(self, name: str, args: tuple[object, ...], ib_id: int | None = None) -> None:
        if not self.connected:
            return
        if self._hold_now:
            self.held.append((name, args))
            return
        self._deliver(name, args)

    def _deliver(self, name: str, args: tuple[object, ...]) -> None:
        assert self.events is not None
        getattr(self.events, name)(*args)
