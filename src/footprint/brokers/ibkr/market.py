"""Live market data from IBKR: the minute bars and the option chain the runner decides on.

The runner asks for `bars(now)` and `chain(now)`; this service answers from what IBKR
has sent, and never from a guess:

Bars
- The traded future is the instrument's nearest quarterly future that is more than
  `roll_days_before_expiry` calendar days from expiry (the exchange's usual roll).
- History (`history_days`, by whole UTC days before today, Saturdays skipped) is loaded
  one day per request. Across a roll it is spliced: each older contract's bars are
  shifted by the price difference between the two contracts at the latest minute both
  traded on its last day (back-adjusted), so levels and averages do not jump at the
  splice, and volume comes from whichever contract was the one traded.
- Today's bars come from polls: `bar_poll_delay_seconds` after each minute closes, the
  recent bars are asked for again (at least `min_poll_seconds`, more after a gap, so a
  reconnect heals itself). Only bars that had closed when the poll was sent are kept.
  No streamed, still-forming bar is ever used.
- Until the whole history has loaded and a first poll has answered, `bars` is empty and
  nothing is decided.

The chain
- Options on the traded future only, at the nearest expiry inside dte_min..dte_max
  (the only expiry the contract picker trades), strikes within `strike_window_points`
  of the future, nearest first, never more than `max_quote_lines` lines in all. The
  window moves once the future has moved `recenter_points`.
- Every tick is stamped when it arrives. The chain holds only quotes no older than
  quotes.max_age_seconds, and is stamped with its oldest quote, so it is never fresher
  than its stalest price. No quote, no chain.
- Quotes reach the shared quote book on the driver's thread (in `step`), never on
  IBKR's, so the book has one writer.

Failing closed: every error from IBKR (no subscription, delayed only, pacing, an
unreadable value) leaves that piece of data missing and is alarmed once per session;
it is retried after `retry_seconds` under a new request id.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import partial
from itertools import pairwise

from footprint.brokers.ibkr.adapter import ContractBook
from footprint.brokers.ibkr.gateway import (
    MARKET_REQUEST_BASE,
    Connection,
    ContractQuery,
    IbBar,
    IbContract,
    IbGreeks,
    IbMarketGateway,
    QuoteField,
)
from footprint.core.calendar import trading_day_at
from footprint.core.config import ExecutionProfile, InstrumentProfile
from footprint.core.models import Bar, require_utc
from footprint.core.options import (
    GreeksVerdict,
    OptionChain,
    OptionGreeks,
    OptionQuote,
    Right,
    judge_greeks,
)
from footprint.execution.orders import Quote

log = logging.getLogger("footprint.ibkr.market")

_MINUTE = timedelta(minutes=1)
_DAY = timedelta(days=1)
_SATURDAY = 5  # date.weekday(): the one UTC day the futures never trade
_NO_DATA = "no data"  # IBKR's error 162 text when a period simply had no trades
_HMDS_ERROR = 162
_RIGHTS = {"C": Right.CALL, "P": Right.PUT}
_FUTURE = "FUT"  # IBKR's security type for a future


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _expiry_date(text: str) -> date | None:
    """IBKR's YYYYMMDD expiry; None for anything else (a month alone, garbage)."""
    if len(text) != len("YYYYMMDD") or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _as_decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _ibkr_end(moment: datetime) -> str:
    """IBKR's endDateTime in UTC: 'yyyymmdd-HH:MM:SS'."""
    return moment.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")


def _conid(contract_id: str) -> int | None:
    """The IBKR conId in the adapter's stand-in id for an unregistered contract."""
    prefix, _, number = contract_id.partition(":")
    if prefix != "conid" or not number.isdigit():
        return None
    return int(number)


def option_contract_id(contract: IbContract) -> str:
    """Ours for an option: readable, and unique by IBKR's conId."""
    strike = f"{contract.strike.normalize():f}"
    return f"{contract.symbol} {contract.expiry} {contract.right}{strike} [{contract.con_id}]"


_Params = tuple[str, str, frozenset[str], frozenset[Decimal]]  # class, size, expiries, strikes


class _Kind(Enum):
    FUTURES = "futures"
    HISTORY = "history"
    POLL = "poll"
    PARAMS = "option params"
    RESOLVE = "held contract lookup"
    EXPIRY = "option contracts"
    UNDERLYING = "future quote"
    OPTION = "option quote"


@dataclass
class _Request:
    kind: _Kind
    sent_at: datetime
    con_id: int = 0
    day: date | None = None
    key: tuple[str, str] | None = None  # the (expiry, trading class) looked up
    contracts: list[IbContract] = field(default_factory=list)
    bars: list[IbBar] = field(default_factory=list)
    params: list[tuple[str, str, frozenset[str], frozenset[Decimal]]] = field(default_factory=list)


@dataclass
class _Line:
    """One streamed quote: a future or an option."""

    contract: IbContract
    contract_id: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_ts: datetime | None = None
    ask_ts: datetime | None = None
    open_interest: int | None = None
    greeks: OptionGreeks | None = None  # IBKR's newest model computation, checked

    @property
    def ts(self) -> datetime | None:
        if self.bid_ts is None or self.ask_ts is None:
            return None
        return min(self.bid_ts, self.ask_ts)  # never fresher than its stalest side

    def quote(self) -> Quote | None:
        ts = self.ts
        if ts is None or self.ask is None or self.ask <= 0:
            return None
        bid = self.bid if self.bid is not None else Decimal(0)  # no bid: 0
        if bid > self.ask:
            return None  # crossed between the two ticks: wait for the next one
        return Quote(self.contract_id, bid, self.ask, ts)


@dataclass(frozen=True, slots=True)
class RecordedOption:
    """One fresh option quote as the recorder reads it (Q3)."""

    quote: Quote  # its own bid, ask and time
    expiry: date
    strike: Decimal
    right: Right
    open_interest: int | None  # None: IBKR sent none


@dataclass(frozen=True, slots=True)
class RecordedChain:
    """What `MarketData.recorded_chain` returns: the future's quote and the options."""

    future: Quote
    options: tuple[RecordedOption, ...]


class HistoryUnavailable(RuntimeError):
    """The history cannot be assembled (a contract missing, a splice with no overlap)."""


class MarketData:
    """IbMarketEvents for the binding; `bars` and `chain` for the runner; `step` for the
    driver (call it every second, on the same thread as the runner)."""

    def __init__(
        self,
        gateway: IbMarketGateway,
        contracts: ContractBook,
        book: Callable[[Quote], object],
        instrument: InstrumentProfile,
        execution: ExecutionProfile,
        alarm: Callable[[str], None],
        clock: Callable[[], datetime] = _utc_now,
        held: Callable[[], Iterable[str]] = tuple,
    ) -> None:
        """`held`: the contract ids the system holds or is working an order on, read every
        step. Their quote streams are never cancelled (window move, expiry change, roll or
        line cap), so an open position is never managed without prices."""
        self._gw = gateway
        self._held = held
        self._pinned: frozenset[str] = frozenset()
        self._contracts = contracts
        self._publish = book
        self._instrument = instrument
        self._s = execution.market_data
        self._option_type = execution.ibkr.option_security_type
        self._max_quote_age = timedelta(seconds=execution.quotes.max_age_seconds)
        self._max_greeks_age = timedelta(seconds=execution.quotes.greeks_max_age_seconds)
        self._alarm_line = alarm
        self._clock = clock
        # Re-entrant: ibapi answers some requests on the caller's own thread (an error
        # when not connected), straight back into this object while it holds the lock.
        self._lock = threading.RLock()
        self._next_req = MARKET_REQUEST_BASE
        # Kept across sessions (facts about the market, not about a connection):
        self._futures: tuple[IbContract, ...] | None = None
        self._raw: dict[int, dict[datetime, Bar]] = {}
        self._loaded: set[tuple[int, date]] = set()  # history days fetched in full
        self._front: IbContract | None = None
        self._series: tuple[Bar, ...] = ()
        self._dirty = True
        self._history_ready = False
        self._complete_until: datetime | None = None
        self._revisions = 0
        self._greeks_rejected = 0
        self._greeks_incomplete = 0
        # Looked up again each trading day: new expiries and strikes are listed daily.
        self._params: dict[tuple[int, date], list[_Params]] = {}
        self._expiry_contracts: dict[tuple[str, str, date], tuple[IbContract, ...]] = {}
        self._broken_history: str | None = None
        self._new_session(connected=False)

    # --- session state ------------------------------------------------------------------

    def _new_session(self, *, connected: bool) -> None:
        """IBKR forgets every request at a disconnect: so does this."""
        self._connected = connected
        self._pending: dict[int, _Request] = {}
        self._lines: dict[int, _Line] = {}  # req id -> streamed quote
        self._underlying: int | None = None  # req id of the future's quote
        self._center: Decimal | None = None
        self._chosen: tuple[str, str] | None = None  # (expiry, trading class)
        self._window: set[int] = set()  # con ids the strike window streams
        self._retry_at: dict[str, datetime] = {}
        self._alarmed: set[str] = set()
        self._last_history_at: datetime | None = None
        self._polled_for: datetime | None = None
        self._outbox: list[Quote] = []
        # (contract, verdict) pairs already logged this session
        self._greeks_warned: set[tuple[str, GreeksVerdict]] = set()

    # --- what the runner reads -------------------------------------------------------------

    def bars(self, now: datetime) -> Sequence[Bar]:
        """Every complete minute bar, oldest first; empty until history and a first poll
        have both arrived."""
        require_utc(now, "now")
        with self._lock:
            if not self._history_ready or self._complete_until is None:
                return ()
            if self._dirty:
                try:
                    self._series = self._assemble(now.date())
                except HistoryUnavailable as exc:
                    self._broken_history = str(exc)
                    self._history_ready = False
                    self._series = ()
                self._dirty = False
            return self._series  # every stored bar is complete: polls keep no other

    def chain(self, now: datetime) -> OptionChain | None:
        """Fresh option quotes on the traded future, or None."""
        require_utc(now, "now")
        with self._lock:
            offered = self._offered(now)
            if offered is None:
                return None
            under, options = offered
            quotes = [
                OptionQuote(
                    contract_id=o.quote.contract_id,
                    expiry=o.expiry,
                    strike=o.strike,
                    right=o.right,
                    bid=o.quote.bid,
                    ask=o.quote.ask,
                    open_interest=o.open_interest or 0,  # unknown: fails the OI check
                )
                for o in options
            ]
            return OptionChain(
                underlying=self._instrument.symbol,
                ts=min([under.ts, *(o.quote.ts for o in options)]),
                underlying_price=(under.bid + under.ask) / 2,
                quotes=tuple(sorted(quotes, key=lambda q: q.contract_id)),
            )

    def recorded_chain(self, now: datetime) -> RecordedChain | None:
        """Q3: the options `chain()` offers at `now`, each with its own quote (and quote
        time) and the open interest IBKR sent (None when it sent none), with the future's
        own quote; None exactly when `chain()` is None. For the read-only recorder: the
        live path does not read it."""
        require_utc(now, "now")
        with self._lock:
            offered = self._offered(now)
            if offered is None:
                return None
            under, options = offered
            ordered = sorted(options, key=lambda o: o.quote.contract_id)
            return RecordedChain(future=under, options=tuple(ordered))

    def _offered(self, now: datetime) -> tuple[Quote, list[RecordedOption]] | None:
        """The fresh future quote and the fresh options on the chosen chain (call under
        the lock); None without a connection, a fresh future bid, or any option."""
        if not self._connected or self._underlying is None:
            return None
        future = self._lines.get(self._underlying)
        if future is None:
            return None
        under = self._fresh(future, now)
        if under is None or under.bid <= 0:
            return None
        options: list[RecordedOption] = []
        for req, line in self._lines.items():
            if req == self._underlying or not self._in_chain(line.contract):
                continue  # a held option from another expiry or future: never offered
            quote = self._fresh(line, now)
            expiry = _expiry_date(line.contract.expiry)
            right = _RIGHTS.get(line.contract.right)
            if quote is None or expiry is None or right is None:
                continue
            options.append(
                RecordedOption(quote, expiry, line.contract.strike, right, line.open_interest)
            )
        if not options:
            return None
        return under, options

    def underlying_quote(self, now: datetime) -> Quote | None:
        """The traded future's quote while fresh (as the chain judges it), or None."""
        require_utc(now, "now")
        with self._lock:
            if not self._connected or self._underlying is None:
                return None
            line = self._lines.get(self._underlying)
            return None if line is None else self._fresh(line, now)

    def option_quote(self, contract_id: str, now: datetime) -> Quote | None:
        """One streamed option's quote while fresh, or None (never the future's)."""
        require_utc(now, "now")
        with self._lock:
            if not self._connected:
                return None
            for req, line in self._lines.items():
                if req != self._underlying and line.contract_id == contract_id:
                    return self._fresh(line, now)
            return None

    def option_greeks(self, contract_id: str, now: datetime) -> OptionGreeks | None:
        """One streamed option's newest usable model Greeks (complete and possible: every
        value present) while no older than `quotes.greeks_max_age_seconds`, or None. Greeks
        age on their own clock: a fresh quote does not refresh an old reading."""
        require_utc(now, "now")
        with self._lock:
            if not self._connected:
                return None
            for req, line in self._lines.items():
                if req != self._underlying and line.contract_id == contract_id:
                    g = line.greeks
                    if g is None or g.ts > now or now - g.ts > self._max_greeks_age:
                        return None
                    return g
            return None

    def _in_chain(self, contract: IbContract) -> bool:
        return self._chosen is not None and (contract.expiry, contract.trading_class) == (
            self._chosen
        )

    def _fresh(self, line: _Line, now: datetime) -> Quote | None:
        quote = line.quote()
        if quote is None or quote.ts > now or now - quote.ts > self._max_quote_age:
            return None
        return quote

    @property
    def front(self) -> IbContract | None:
        with self._lock:
            return self._front

    @property
    def revisions(self) -> int:
        """Bars IBKR changed after a poll had already counted them complete."""
        with self._lock:
            return self._revisions

    @property
    def greeks_rejected(self) -> int:
        """Model readings IBKR sent that are unreadable or impossible for the option (each
        cleared the reading before it)."""
        with self._lock:
            return self._greeks_rejected

    @property
    def greeks_incomplete(self) -> int:
        """Model readings with some values computed and some not (each cleared the
        reading before it: only a complete reading is usable)."""
        with self._lock:
            return self._greeks_incomplete

    # --- IbMarketEvents (IBKR's message thread) ------------------------------------------

    def on_market_connection(self, state: Connection) -> None:
        with self._lock:
            self._new_session(connected=state is Connection.CONNECTED)
        log.info("market data: %s", state.value)

    def on_market_notice(self, code: int, message: str) -> None:
        log.warning("IBKR notice %s: %s", code, message)

    def on_contract_details(self, req_id: int, contract: IbContract) -> None:
        with self._lock:
            request = self._pending.get(req_id)
            if request is not None:
                request.contracts.append(contract)

    def on_contract_details_end(self, req_id: int) -> None:
        with self._lock:
            request = self._pending.pop(req_id, None)
            if request is None:
                return
            if request.kind is _Kind.FUTURES:
                self._take_futures(request.contracts)
            elif request.kind is _Kind.RESOLVE:
                self._take_resolved(request)
            elif request.kind is _Kind.EXPIRY and request.key is not None:
                assert request.day is not None
                self._take_expiry(request.key, request.day, request.contracts)

    def on_option_params(
        self,
        req_id: int,
        exchange: str,
        trading_class: str,
        multiplier: str,
        expirations: frozenset[str],
        strikes: frozenset[Decimal],
    ) -> None:
        with self._lock:
            request = self._pending.get(req_id)
            if request is not None and exchange == self._instrument.exchange:
                request.params.append((trading_class, multiplier, expirations, strikes))

    def on_option_params_end(self, req_id: int) -> None:
        with self._lock:
            request = self._pending.pop(req_id, None)
            if request is not None:
                assert request.day is not None
                day = request.day
                self._params = {k: v for k, v in self._params.items() if k[1] == day}
                self._params[(request.con_id, day)] = request.params

    def on_quote(self, req_id: int, field: QuoteField, value: Decimal | None) -> None:
        stamp = self._clock()
        with self._lock:
            line = self._lines.get(req_id)
            if line is None:
                return  # a cancelled or older stream's late tick
            if field is QuoteField.BID:
                line.bid, line.bid_ts = value, stamp
            elif field is QuoteField.ASK:
                line.ask, line.ask_ts = value, stamp
            else:
                line.open_interest = int(value) if value is not None else None
            if req_id != self._underlying:
                quote = line.quote()
                if quote is not None:
                    self._outbox.append(quote)

    def on_greeks(self, req_id: int, greeks: IbGreeks) -> None:
        """Keep the reading only if it is usable (complete and possible); otherwise clear
        the one before it (it is no longer the market's). An unreadable, impossible or
        incomplete reading is counted; a "nothing computed yet" one is not. The future's
        line takes none."""
        stamp = self._clock()
        with self._lock:
            line = self._lines.get(req_id)
            if line is None or req_id == self._underlying:
                return  # a cancelled or older stream's late reading, or the future
            right = _RIGHTS.get(line.contract.right)
            if greeks.unreadable or right is None:
                verdict, checked = GreeksVerdict.IMPOSSIBLE, None
            else:
                verdict, checked = judge_greeks(
                    right,
                    stamp,
                    implied_vol=greeks.implied_vol,
                    delta=greeks.delta,
                    gamma=greeks.gamma,
                    vega=greeks.vega,
                    theta=greeks.theta,
                    underlying_price=greeks.underlying_price,
                )
            line.greeks = checked
            if verdict is GreeksVerdict.IMPOSSIBLE:
                self._greeks_rejected += 1
            elif verdict is GreeksVerdict.INCOMPLETE:
                self._greeks_incomplete += 1
            else:
                return
            key = (line.contract_id, verdict)
            if key not in self._greeks_warned:  # once per contract and kind per session
                self._greeks_warned.add(key)
                log.warning(
                    "%s Greeks for %s dropped (only the first is logged this session): %s",
                    verdict.value.lower(),
                    line.contract_id,
                    greeks,
                )

    def on_bar(self, req_id: int, bar: IbBar) -> None:
        with self._lock:
            request = self._pending.get(req_id)
            if request is not None:
                request.bars.append(bar)

    def on_bars_end(self, req_id: int) -> None:
        with self._lock:
            request = self._pending.pop(req_id, None)
            if request is not None:
                self._take_bars(request)

    def on_bar_update(self, req_id: int, bar: IbBar) -> None:
        """Never asked for (no bar is streamed); ignored."""

    def on_market_error(self, req_id: int, code: int, message: str) -> None:
        with self._lock:
            request = self._pending.pop(req_id, None)
            if request is not None:
                if (
                    request.kind in (_Kind.HISTORY, _Kind.POLL)
                    and code == _HMDS_ERROR
                    and _NO_DATA in message.lower()
                ):
                    self._take_bars(request)  # a period with no trades: an empty answer
                    return
                self._failed(request.kind.value, f"IBKR {code}: {message}")
                return
            line = self._lines.pop(req_id, None)
            if line is None:
                return  # an old request's late error
            if req_id == self._underlying:
                self._underlying = None
                what = _Kind.UNDERLYING.value
            elif line.contract_id in self._pinned:
                what = f"held {line.contract_id}"  # asked again by _ensure_held
            else:
                self._center = None  # resubscribe the window on the next step
                what = _Kind.OPTION.value
            self._failed(what, f"IBKR {code}: {message} ({line.contract_id})")

    # --- answers ----------------------------------------------------------------------

    def _take_futures(self, contracts: list[IbContract]) -> None:
        mine = []
        for c in contracts:
            if (
                c.sec_type == _FUTURE
                and c.symbol == self._instrument.symbol
                and _expiry_date(c.expiry) is not None
                and _as_decimal(c.multiplier) == self._instrument.point_value
            ):
                mine.append(c)
        self._futures = tuple(sorted(mine, key=lambda c: c.expiry))
        if not self._futures:
            self._failed(_Kind.FUTURES.value, "IBKR listed no usable futures")
            self._futures = None

    def _take_expiry(self, chosen: tuple[str, str], day: date, contracts: list[IbContract]) -> None:
        usable = tuple(
            c
            for c in contracts
            if c.expiry == chosen[0]
            and c.trading_class == chosen[1]
            and c.right in _RIGHTS
            and c.strike > 0
            and _as_decimal(c.multiplier) == self._instrument.point_value
        )
        self._expiry_contracts = {k: v for k, v in self._expiry_contracts.items() if k[2] == day}
        self._expiry_contracts[(chosen[0], chosen[1], day)] = usable
        if not usable:
            self._failed(_Kind.EXPIRY.value, f"no usable options for {chosen[1]} {chosen[0]}")

    def _take_bars(self, request: _Request) -> None:
        if request.kind is _Kind.HISTORY:
            store = self._raw.setdefault(request.con_id, {})
            for b in request.bars:
                if b.ts + _MINUTE <= request.sent_at:  # a day still under way keeps no partial
                    store[b.ts] = self._bar(b)
            assert request.day is not None
            self._loaded.add((request.con_id, request.day))
            self._dirty = True
            return
        # A poll: a bar counts as complete only once IBKR itself has moved on (a later
        # minute in the same answer: IBKR's clock, not ours) and it had closed by ours too.
        store = self._raw.setdefault(request.con_id, {})
        latest = max((b.ts for b in request.bars), default=None)
        for b in request.bars:
            if latest is None or b.ts >= latest or b.ts + _MINUTE > request.sent_at:
                continue
            bar = self._bar(b)
            old = store.get(b.ts)
            done = self._complete_until
            if old is not None and old != bar and done is not None and b.ts + _MINUTE <= done:
                self._revisions += 1
                log.warning("IBKR revised the %s bar after it was counted complete", b.ts)
            store[b.ts] = bar
        if self._complete_until is None or request.sent_at > self._complete_until:
            self._complete_until = request.sent_at
        self._dirty = True

    def _bar(self, b: IbBar) -> Bar:
        return Bar(self._instrument.symbol, b.ts, b.open, b.high, b.low, b.close, b.volume)

    def _failed(self, what: str, why: str) -> None:
        self._retry_at[what] = self._clock() + timedelta(seconds=self._s.retry_seconds)
        self._alarm_once(
            what,
            f"MARKET DATA: {what} failed ({why}); retrying in "
            f"{self._s.retry_seconds}s. Until then that data is missing",
        )

    def _alarm_once(self, key: str, message: str) -> None:
        if key in self._alarmed:
            log.debug(message)  # alarmed already this session: not again every step
            return
        self._alarmed.add(key)
        try:
            self._alarm_line(message)
        except Exception:
            log.exception("could not deliver a market data alarm")

    # --- the driver -------------------------------------------------------------------

    def step(self, now: datetime) -> None:
        """Publish the quotes that arrived, then ask IBKR for whatever is missing."""
        require_utc(now, "now")
        with self._lock:
            outbox, self._outbox = self._outbox, []
        for quote in outbox:
            try:
                self._publish(quote)
            except Exception:
                log.exception("the quote book refused a quote")
        with self._lock:
            if not self._connected:
                return
            self._expire(now)
            self._read_held()  # held options first: they need no future looked up
            self._ensure_held(now)
            if self._futures is None:
                self._ask_futures(now)
                if self._futures is None:
                    return
            self._follow_front(now)
            if self._front is None:
                return
            self._ensure_underlying(now)
            self._ensure_options(now)
            self._release(now)
            self._ensure_bars(now)

    def _read_held(self) -> None:
        try:
            self._pinned = frozenset(self._held())
        except Exception as exc:  # keep the last known set: never drop a held stream
            log.exception("could not read the held contracts")
            self._alarm_once("held", f"MARKET DATA: could not read the held contracts ({exc!r})")

    def _ensure_held(self, now: datetime) -> None:
        """Every held option is streamed, whatever the window, expiry or roll."""
        streaming = {
            line.contract_id for req, line in self._lines.items() if req != self._underlying
        }
        for contract_id in sorted(self._pinned - streaming):
            if not self._ready(f"held {contract_id}", now):
                continue
            try:
                contract = self._contracts.ib(contract_id)
            except KeyError:
                self._resolve(contract_id, now)  # e.g. held from before a restart
                continue
            req = self._claim()
            self._lines[req] = _Line(contract, contract_id)
            if not self._call(partial(self._gw.request_quotes, req, contract)):
                self._lines.pop(req, None)
                self._failed(f"held {contract_id}", "could not be sent")

    def _resolve(self, contract_id: str, now: datetime) -> None:
        """Look up a held contract the system never registered: the adapter names it
        `conid:<IBKR conId>`. Once found, it is registered under that same id, so the
        adapter, reconcile and this service all agree on it."""
        con_id = _conid(contract_id)
        if con_id is None:
            self._alarm_once(
                f"held {contract_id}",
                f"MARKET DATA: cannot stream the held {contract_id}: not a known contract",
            )
            return
        busy = any(
            r.kind is _Kind.RESOLVE and r.key == (contract_id, "") for r in self._pending.values()
        )
        if busy:
            return
        query = ContractQuery(
            self._instrument.symbol,
            self._option_type,
            self._instrument.exchange,
            self._instrument.currency,
            con_id=con_id,
        )
        self._send(
            _Request(_Kind.RESOLVE, now, con_id=con_id, key=(contract_id, "")),
            lambda r: self._gw.request_contract_details(r, query),
        )

    def _take_resolved(self, request: _Request) -> None:
        assert request.key is not None
        contract_id = request.key[0]
        found = [
            c
            for c in request.contracts
            if c.con_id == request.con_id
            and _as_decimal(c.multiplier) == self._instrument.point_value
        ]
        if len(found) != 1:
            self._failed(
                f"held {contract_id}",
                f"IBKR does not know {contract_id} as one of the instrument's options",
            )
            return
        self._contracts.register(contract_id, found[0])

    def _release(self, now: datetime) -> None:
        """A stream no longer held and outside the window is cancelled."""
        for req, line in list(self._lines.items()):
            if req == self._underlying or line.contract_id in self._pinned:
                continue
            if line.contract.con_id in self._window:  # (window ids are all in the chain)
                continue
            self._call(partial(self._gw.cancel_quotes, req))
            self._lines.pop(req, None)

    def _expire(self, now: datetime) -> None:
        timeout = timedelta(seconds=self._s.request_timeout_seconds)
        for req, request in list(self._pending.items()):
            if now - request.sent_at > timeout:
                del self._pending[req]
                if request.kind in (_Kind.HISTORY, _Kind.POLL):
                    self._call(partial(self._gw.cancel_bars, req))
                self._failed(request.kind.value, f"no answer in {timeout}")

    def _ready(self, what: str, now: datetime) -> bool:
        at = self._retry_at.get(what)
        return at is None or now >= at

    def _busy(self, *kinds: _Kind) -> bool:
        return any(r.kind in kinds for r in self._pending.values())

    def _claim(self) -> int:
        req = self._next_req
        self._next_req += 1
        return req

    def _call(self, action: Callable[[], None]) -> bool:
        try:
            action()
        except (ConnectionError, ValueError) as exc:
            log.warning("market data request not sent: %s", exc)
            return False
        return True

    def _send(self, request: _Request, action: Callable[[int], None]) -> None:
        req = self._claim()
        self._pending[req] = request  # before the call: an answer can come during it
        if not self._call(lambda: action(req)):
            self._pending.pop(req, None)
            self._failed(request.kind.value, "could not be sent")

    def _query(self, contract: IbContract) -> ContractQuery:
        return ContractQuery(
            contract.symbol,
            contract.sec_type,
            contract.exchange,
            contract.currency,
            contract.expiry,
            con_id=contract.con_id,
            include_expired=True,
        )

    # futures and the roll

    def _ask_futures(self, now: datetime) -> None:
        if self._busy(_Kind.FUTURES) or not self._ready(_Kind.FUTURES.value, now):
            return
        query = ContractQuery(
            self._instrument.symbol,
            _FUTURE,
            self._instrument.exchange,
            self._instrument.currency,
            include_expired=True,
        )
        self._send(
            _Request(_Kind.FUTURES, now), lambda r: self._gw.request_contract_details(r, query)
        )

    def front_for(self, day: date) -> IbContract | None:
        """The future traded on `day`: the nearest one more than the roll from expiry."""
        roll = timedelta(days=self._s.roll_days_before_expiry)
        for c in self._futures or ():
            expiry = _expiry_date(c.expiry)
            if expiry is not None and day < expiry - roll:
                return c
        return None

    def _follow_front(self, now: datetime) -> None:
        front = self.front_for(now.date())
        if front is None:
            self._alarm_once("front", "MARKET DATA: no future to trade (none listed past the roll)")
            return
        if self._front is not None and front.con_id == self._front.con_id:
            return
        if self._front is not None:
            log.warning("rolling from %s to %s", self._front.expiry, front.expiry)
        self._cancel_all_lines()
        self._front = front
        self._history_ready = False
        self._complete_until = None
        self._broken_history = None
        self._dirty = True

    def _cancel_all_lines(self) -> None:
        """At a roll: everything but the held options' streams."""
        for req, line in list(self._lines.items()):
            if req != self._underlying and line.contract_id in self._pinned:
                continue
            self._call(partial(self._gw.cancel_quotes, req))
            self._lines.pop(req, None)
        self._underlying = None
        self._center = None
        self._chosen = None
        self._window = set()

    # the future's quote

    def _ensure_underlying(self, now: datetime) -> None:
        if self._underlying is not None or not self._ready(_Kind.UNDERLYING.value, now):
            return
        assert self._front is not None
        front = self._front
        req = self._claim()
        self._lines[req] = _Line(front, f"{front.symbol} {front.expiry} future")
        self._underlying = req  # before the call: an answer can come during it
        if not self._call(lambda: self._gw.request_quotes(req, front)):
            self._lines.pop(req, None)
            if self._underlying == req:
                self._underlying = None
            self._failed(_Kind.UNDERLYING.value, "could not be sent")

    # options

    def _ensure_options(self, now: datetime) -> None:
        assert self._front is not None
        front = self._front
        today = trading_day_at(now, self._instrument)
        if today is None:
            return  # market closed: keep whatever is streaming
        params = self._params.get((front.con_id, today))
        if params is None:
            if not self._busy(_Kind.PARAMS) and self._ready(_Kind.PARAMS.value, now):
                self._send(
                    _Request(_Kind.PARAMS, now, con_id=front.con_id, day=today),
                    lambda r: self._gw.request_option_params(
                        r, front.symbol, self._instrument.exchange, _FUTURE, front.con_id
                    ),
                )
            return
        chosen = self._choose_expiry(params, today)
        if chosen is None:
            self._alarm_once(
                f"expiry {today}",
                f"MARKET DATA: no option on the {front.expiry} future expires in "
                f"{self._instrument.dte_min}-{self._instrument.dte_max} days of {today}",
            )
            return
        if chosen != self._chosen:
            self._drop_option_lines()
            self._chosen = chosen
        contracts = self._expiry_contracts.get((chosen[0], chosen[1], today))
        if contracts is None:
            if not self._busy(_Kind.EXPIRY) and self._ready(_Kind.EXPIRY.value, now):
                query = ContractQuery(
                    front.symbol,
                    self._option_type,
                    self._instrument.exchange,
                    self._instrument.currency,
                    expiry=chosen[0],
                    trading_class=chosen[1],
                )
                self._send(
                    _Request(_Kind.EXPIRY, now, day=today, key=chosen),
                    lambda r: self._gw.request_contract_details(r, query),
                )
            return
        self._ensure_window(contracts, now)

    def _choose_expiry(
        self, params: list[tuple[str, str, frozenset[str], frozenset[Decimal]]], today: date
    ) -> tuple[str, str] | None:
        """The nearest expiry in the DTE range; if several trading classes list it, the
        one with the most strikes (then by name), so the choice is always the same."""
        lo, hi = self._instrument.dte_min, self._instrument.dte_max
        best: tuple[date, int, str, str] | None = None
        for trading_class, multiplier, expirations, strikes in params:
            if _as_decimal(multiplier) != self._instrument.point_value:
                continue
            for text in expirations:
                expiry = _expiry_date(text)
                if expiry is None or not lo <= (expiry - today).days <= hi:
                    continue
                key = (expiry, -len(strikes), trading_class, text)
                if best is None or key < best:
                    best = key
        return None if best is None else (best[3], best[2])

    def _drop_option_lines(self) -> None:
        for req, line in list(self._lines.items()):
            if req == self._underlying or line.contract_id in self._pinned:
                continue
            self._call(partial(self._gw.cancel_quotes, req))
            self._lines.pop(req, None)
        self._center = None
        self._window = set()

    def _ensure_window(self, contracts: tuple[IbContract, ...], now: datetime) -> None:
        if self._underlying is None:
            return
        under = self._fresh(self._lines[self._underlying], now)
        if under is None or under.bid <= 0:
            return
        mid = (under.bid + under.ask) / 2
        if self._center is not None and abs(mid - self._center) <= self._s.recenter_points:
            return
        if not self._ready(_Kind.OPTION.value, now):
            return
        held = {
            line.contract.con_id
            for req, line in self._lines.items()
            if req != self._underlying and line.contract_id in self._pinned
        }
        room = max(0, self._s.max_quote_lines - 1 - len(held))  # the future holds one line
        inside = [
            c
            for c in contracts
            if abs(c.strike - mid) <= self._s.strike_window_points and c.con_id not in held
        ]
        inside.sort(key=lambda c: (abs(c.strike - mid), c.strike, c.right))
        wanted = {c.con_id: c for c in inside[:room]}
        if len(inside) > room:
            self._alarm_once(
                "lines",
                f"MARKET DATA: {len(inside)} strikes in the window; streaming the "
                f"nearest {room} (max_quote_lines)",
            )
        current = {
            line.contract.con_id: req
            for req, line in self._lines.items()
            if req != self._underlying and line.contract.con_id not in held
        }
        for con_id, req in current.items():
            if con_id not in wanted:
                self._call(partial(self._gw.cancel_quotes, req))
                self._lines.pop(req, None)
        for con_id, contract in wanted.items():
            if con_id in current:
                continue
            contract_id = option_contract_id(contract)
            self._contracts.register(contract_id, contract)
            req = self._claim()
            self._lines[req] = _Line(contract, contract_id)
            if not self._call(partial(self._gw.request_quotes, req, contract)):
                self._lines.pop(req, None)
                self._failed(_Kind.OPTION.value, "could not be sent")
                return
        self._window = set(wanted) | {  # a held strike counts only while inside
            c.con_id
            for c in contracts
            if c.con_id in held and abs(c.strike - mid) <= self._s.strike_window_points
        }
        streaming = {line.contract.con_id for line in self._lines.values()}
        if all(con_id in streaming for con_id in wanted):  # none refused during the calls
            self._center = mid

    # bars

    def _ensure_bars(self, now: datetime) -> None:
        if self._busy(_Kind.HISTORY, _Kind.POLL):
            return
        gap = timedelta(seconds=self._s.history_request_gap_seconds)
        if self._last_history_at is not None and now - self._last_history_at < gap:
            return
        if not self._history_ready:
            if self._broken_history is not None:
                self._failed("history", self._broken_history)
                self._broken_history = None
                self._forget_empty_days()  # a day that came back empty is asked for again
                return
            if self._ready(_Kind.HISTORY.value, now):
                self._next_history(now)
            return
        boundary = now.replace(second=0, microsecond=0)
        if now - boundary < timedelta(seconds=self._s.bar_poll_delay_seconds):
            return
        if self._polled_for is not None and self._polled_for >= boundary:
            return
        if not self._ready(_Kind.POLL.value, now):
            return
        self._poll(now, boundary)

    def _history_days(self, today: date) -> list[date]:
        first = today - timedelta(days=self._s.history_days)
        days = []
        day = first
        while day < today:
            if day.weekday() != _SATURDAY:
                days.append(day)
            day += _DAY
        return days

    def _segments(self, today: date) -> list[tuple[IbContract, list[date]]]:
        """The history days grouped by the future traded on them, oldest first; the last
        segment is always today's future (its days may be none: a roll today)."""
        assert self._front is not None
        segments: list[tuple[IbContract, list[date]]] = []
        for day in self._history_days(today):
            owner = self.front_for(day)
            if owner is None:
                raise HistoryUnavailable(f"no future was listed as traded on {day}")
            if segments and segments[-1][0].con_id == owner.con_id:
                segments[-1][1].append(day)
            else:
                segments.append((owner, [day]))
        if not segments or segments[-1][0].con_id != self._front.con_id:
            segments.append((self._front, []))
        return segments

    def _plan(self, today: date) -> list[tuple[IbContract, date]]:
        """Every (contract, UTC day) the history needs, oldest first: each day from the
        future traded on it, then each splice day's bars from the newer future."""
        segments = self._segments(today)
        need = [(contract, day) for contract, days in segments for day in days]
        for (old, days), (newer, _) in pairwise(segments):
            need.append((newer, self._splice_day(old, days)))
        return need

    def _splice_day(self, old: IbContract, days: list[date]) -> date:
        """The old future's last day with bars (a holiday has none)."""
        store = self._raw.get(old.con_id, {})
        traded = {ts.date() for ts in store}
        for day in reversed(days):
            if day in traded:
                return day
        return days[-1]

    def _next_history(self, now: datetime) -> None:
        try:
            plan = self._plan(now.date())
        except HistoryUnavailable as exc:
            self._broken_history = str(exc)
            return
        for contract, day in plan:
            if (contract.con_id, day) in self._loaded:
                continue
            end = datetime.combine(day + _DAY, time(0), tzinfo=UTC)
            self._send_bars(_Kind.HISTORY, contract, day, end, _DAY, now)
            return
        self._history_ready = True
        self._dirty = True
        log.info("market data: history loaded (%s days)", self._s.history_days)

    def _is_loaded(self, day: date) -> bool:
        owner = self.front_for(day)
        return owner is not None and (owner.con_id, day) in self._loaded

    def _forget_empty_days(self) -> None:
        for con_id, day in list(self._loaded):
            store = self._raw.get(con_id, {})
            if not any(ts.date() == day for ts in store):
                self._loaded.discard((con_id, day))

    def _poll(self, now: datetime, boundary: datetime) -> None:
        assert self._front is not None
        store = self._raw.get(self._front.con_id, {})
        newest = max(store) if store else now
        span = max(now - newest + _MINUTE, timedelta(seconds=self._s.min_poll_seconds))
        if span > _DAY:  # IBKR's limit for one request; a longer outage needs whole days
            unloaded = [
                day
                for day in self._history_days(now.date())
                if day >= newest.date() and not self._is_loaded(day)
            ]
            if unloaded:
                self._history_ready = False  # load them, then poll
                return
            span = _DAY  # every earlier day is loaded: no hole
        self._polled_for = boundary
        self._send_bars(_Kind.POLL, self._front, None, None, span, now)

    def _send_bars(
        self,
        kind: _Kind,
        contract: IbContract,
        day: date | None,
        end: datetime | None,
        span: timedelta,
        now: datetime,
    ) -> None:
        self._last_history_at = now
        query = self._query(contract)
        end_text = "" if end is None else _ibkr_end(end)
        duration = f"{int(span.total_seconds())} S"
        self._send(
            _Request(kind, now, con_id=contract.con_id, day=day),
            lambda r: self._gw.request_bars(r, query, end_text, duration, False),
        )

    def _assemble(self, today: date) -> tuple[Bar, ...]:
        """Today's contract's bars, with each older contract's history spliced on before
        them, back-adjusted at the latest minute both contracts traded."""
        assert self._front is not None
        segments = self._segments(today)
        days = self._history_days(today)
        newer = self._raw.get(segments[-1][0].con_id, {})
        series: dict[datetime, Bar] = dict(newer)
        offset = Decimal(0)
        for old_contract, old_days in reversed(segments[:-1]):
            old = self._raw.get(old_contract.con_id, {})
            splice_days = set(old_days)
            common = sorted(ts for ts in old if ts in newer and ts.date() in splice_days)
            if not common:
                raise HistoryUnavailable(
                    f"cannot splice the {old_contract.expiry} future onto the next: "
                    "no minute both traded on its last days"
                )
            cut = common[-1]
            offset += newer[cut].close - old[cut].close
            series = {ts: b for ts, b in series.items() if ts > cut}
            for ts, b in old.items():
                if ts <= cut:
                    series[ts] = self._shift(b, offset)
            newer = old
        start = datetime.combine(days[0] if days else today, time(0), tzinfo=UTC)
        return tuple(series[ts] for ts in sorted(series) if ts >= start)

    def _shift(self, b: Bar, offset: Decimal) -> Bar:
        try:
            return Bar(
                b.symbol,
                b.ts,
                b.open + offset,
                b.high + offset,
                b.low + offset,
                b.close + offset,
                b.volume,
            )
        except ValueError as exc:
            raise HistoryUnavailable(f"a back-adjusted bar is not a bar ({exc})") from exc
