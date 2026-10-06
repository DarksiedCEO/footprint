"""The live market data service: what the runner gets as bars and chains, built only
from what IBKR answered, and nothing when IBKR has not answered."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest

from footprint.brokers.ibkr.adapter import ContractBook
from footprint.brokers.ibkr.gateway import (
    MARKET_REQUEST_BASE,
    Connection,
    ContractQuery,
    IbBar,
    IbContract,
    QuoteField,
)
from footprint.brokers.ibkr.market import MarketData, option_contract_id
from footprint.core.config import ExecutionProfile, InstrumentProfile
from footprint.core.options import Right
from footprint.execution.orders import Quote

D = Decimal
MINUTE = timedelta(minutes=1)
SEP = IbContract(400, "NQ", "FUT", "CME", "USD", "20260918", D(0), "", "20", "NQ")
DEC = IbContract(500, "NQ", "FUT", "CME", "USD", "20261218", D(0), "", "20", "NQ")
MAR = IbContract(600, "NQ", "FUT", "CME", "USD", "20270319", D(0), "", "20", "NQ")
MICRO = IbContract(700, "NQ", "FUT", "CME", "USD", "20261218", D(0), "", "2", "NQ")  # wrong size
BASE = {SEP.con_id: D(19900), DEC.con_id: D(20000), MAR.con_id: D(20150)}
# Tuesday 2026-10-06 07:00 PT: prime session, trading day Oct 6 (DTE 1-3: Oct 7, 8, 9).
TUESDAY = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)


def opt(con_id: int, expiry: str, strike: int, right: str, tc: str = "Q2C") -> IbContract:
    return IbContract(con_id, "NQ", "FOP", "CME", "USD", expiry, D(strike), right, "20", tc)


def price(con_id: int, ts: datetime) -> Decimal:
    """A deterministic market: each contract drifts a quarter point a minute."""
    minutes = int((ts - datetime(2026, 1, 1, tzinfo=UTC)) / MINUTE)
    return BASE[con_id] + D(minutes % 400) / 4


def ib_bar(con_id: int, ts: datetime, volume: int = 10) -> IbBar:
    p = price(con_id, ts)
    return IbBar(ts, p, p + 1, p - 1, p, volume)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Gateway:
    """IbMarketGateway, recorded; `answer` is called for every request as it is sent."""

    sent: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    answer: Any = None

    def _record(self, name: str, *args: Any) -> None:
        self.sent.append((name, args))
        if self.answer is not None:
            self.answer(name, args)

    def request_contract_details(self, req_id: int, query: ContractQuery) -> None:
        self._record("details", req_id, query)

    def request_option_params(self, *args: Any) -> None:
        self._record("params", *args)

    def request_quotes(self, req_id: int, contract: IbContract) -> None:
        self._record("quotes", req_id, contract)

    def cancel_quotes(self, req_id: int) -> None:
        self._record("cancel_quotes", req_id)

    def request_bars(self, *args: Any) -> None:
        self._record("bars", *args)

    def cancel_bars(self, req_id: int) -> None:
        self._record("cancel_bars", req_id)

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.sent if n == name]


class World:
    """The service wired to a fake IBKR that answers lookups and history at once
    (unless told otherwise); quotes are fed by hand."""

    def __init__(
        self,
        execution: ExecutionProfile,
        instrument: InstrumentProfile,
        now: datetime = TUESDAY,
        futures: tuple[IbContract, ...] = (SEP, DEC, MAR),
        options: tuple[IbContract, ...] = (),
        params: list[tuple[str, str, frozenset[str], frozenset[Decimal]]] | None = None,
        **market: Any,
    ) -> None:
        settings = replace(execution.market_data, history_days=3, **market)
        self.execution = replace(execution, market_data=settings)
        self.instrument = instrument
        self.clock = Clock(now)
        self.gw = Gateway(answer=self._answer)
        self.contracts = ContractBook()
        self.published: list[Quote] = []
        self.alarms: list[str] = []
        self.futures = futures
        self.options = options
        self.params = (
            params
            if params is not None
            else [("Q2C", "20", frozenset({"20261007", "20261008"}), frozenset({D(25000)}))]
        )
        self.no_answer: set[str] = set()
        self.missing: set[tuple[int, date]] = set()  # (con_id, day) with no trades at all
        self.md = MarketData(
            self.gw,
            self.contracts,
            self.published.append,
            instrument,
            self.execution,
            self.alarms.append,
            self.clock,
        )

    def _answer(self, name: str, args: tuple[Any, ...]) -> None:
        if name in self.no_answer:
            return
        md = self.md
        if name == "details":
            req, query = args
            found = self.futures if query.sec_type == "FUT" else self.options
            for c in found:
                if query.sec_type != "FUT" and (
                    c.expiry != query.expiry or c.trading_class != query.trading_class
                ):
                    continue
                md.on_contract_details(req, c)
            md.on_contract_details_end(req)
        elif name == "params":
            req = args[0]
            for tc, mult, expirations, strikes in self.params:
                md.on_option_params(req, "CME", tc, mult, expirations, strikes)
            md.on_option_params_end(req)
        elif name == "bars":
            req, query, end, duration, keep = args
            assert keep is False
            seconds = int(duration.split()[0])
            stop = (
                self.clock.now
                if end == ""
                else datetime.strptime(end + "+0000", "%Y%m%d-%H:%M:%S%z")
            )
            stop = min(stop.replace(tzinfo=UTC), self.clock.now)  # IBKR has nothing later
            ts = (stop - timedelta(seconds=seconds)).replace(second=0, microsecond=0)
            while ts < stop:
                if (query.con_id, ts.date()) not in self.missing:
                    md.on_bar(req, ib_bar(query.con_id, ts))
                ts += MINUTE
            md.on_bars_end(req)

    def run(self, seconds: int) -> None:
        for _ in range(seconds):
            self.md.step(self.clock.now)
            self.clock.now += timedelta(seconds=1)

    def connect(self) -> None:
        self.md.on_market_connection(Connection.CONNECTED)

    def tick(self, contract: IbContract, bid: Decimal | None, ask: Decimal | None) -> None:
        req = self.line(contract)
        self.md.on_quote(req, QuoteField.BID, bid)
        self.md.on_quote(req, QuoteField.ASK, ask)

    def line(self, contract: IbContract) -> int:
        """The newest quote request for `contract`."""
        reqs = [args[0] for args in self.gw.named("quotes") if args[1].con_id == contract.con_id]
        assert reqs, f"no quote request for {contract.con_id}"
        return int(reqs[-1])

    def history_requests(self) -> list[tuple[int, date, str]]:
        out = []
        for _req, query, end, duration, _ in self.gw.named("bars"):
            if end:
                out.append(
                    (
                        query.con_id,
                        datetime.strptime(end[:8] + "+0000", "%Y%m%d%z").date(),
                        duration,
                    )
                )
        return out


@pytest.fixture
def world(execution: ExecutionProfile, instrument: InstrumentProfile) -> World:
    return World(execution, instrument)


def ready(w: World) -> None:
    """Connected, history loaded and the first poll answered."""
    w.connect()
    w.run(20)
    w.clock.now = w.clock.now.replace(second=0) + MINUTE + timedelta(seconds=6)
    w.run(2)
    assert w.md.bars(w.clock.now), "not ready"


# --- start-up --------------------------------------------------------------------------


def test_nothing_is_asked_before_the_connection_opens(world: World) -> None:
    world.run(5)
    assert world.gw.sent == []
    assert world.md.bars(world.clock.now) == ()
    assert world.md.chain(world.clock.now) is None


def test_the_futures_are_looked_up_first_including_expired_ones(world: World) -> None:
    world.no_answer.add("details")
    world.connect()
    world.run(3)
    ((req, query),) = world.gw.named("details")  # asked once while it is outstanding
    assert req >= MARKET_REQUEST_BASE
    assert (query.symbol, query.sec_type, query.exchange, query.currency) == (
        "NQ",
        "FUT",
        "CME",
        "USD",
    )
    assert query.include_expired
    assert [n for n, _ in world.gw.sent] == ["details"]


def test_the_front_is_the_nearest_future_more_than_the_roll_from_expiry(world: World) -> None:
    world.connect()
    world.run(1)
    assert world.md.front == DEC
    assert world.md.front_for(date(2026, 9, 9)) == SEP  # 9 days before Sep 18
    assert world.md.front_for(date(2026, 9, 10)) == DEC  # 8 days before: rolled
    assert world.md.front_for(date(2026, 12, 9)) == DEC
    assert world.md.front_for(date(2026, 12, 10)) == MAR


def test_a_future_of_the_wrong_size_is_never_traded(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, futures=(MICRO, DEC))
    w.connect()
    w.run(1)
    assert w.md.front == DEC


def test_no_usable_future_is_alarmed_and_retried(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, futures=(MICRO,), retry_seconds=10)
    w.connect()
    w.run(5)
    assert len(w.gw.named("details")) == 1
    assert sum("futures failed" in a for a in w.alarms) == 1
    w.run(10)
    assert len(w.gw.named("details")) == 2  # tried again after retry_seconds


def test_history_loads_one_utc_day_per_request_skipping_saturdays(world: World) -> None:
    world.connect()
    world.run(20)
    # Tuesday Oct 6, 3 days back: Oct 3 (a Saturday) skipped, Oct 4 and Oct 5 loaded.
    assert world.history_requests() == [
        (DEC.con_id, date(2026, 10, 5), "86400 S"),  # end = the next UTC midnight
        (DEC.con_id, date(2026, 10, 6), "86400 S"),
    ]
    first = world.gw.named("bars")[0]
    assert first[1].include_expired
    assert first[1].con_id == DEC.con_id


def test_history_requests_are_spaced_and_one_at_a_time(world: World) -> None:
    world.no_answer.add("bars")
    world.connect()
    world.run(10)
    assert len(world.gw.named("bars")) == 1  # the first is still unanswered
    world.no_answer.clear()


def test_history_requests_wait_the_pacing_gap(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, history_request_gap_seconds=5)
    w.connect()
    sent_at = []
    for _ in range(20):
        before = len(w.gw.named("bars"))
        w.md.step(w.clock.now)
        if len(w.gw.named("bars")) > before:
            sent_at.append(w.clock.now)
        w.clock.now += timedelta(seconds=1)
    assert len(sent_at) >= 2
    assert all(b - a >= timedelta(seconds=5) for a, b in pairwise(sent_at))


def test_no_bars_until_history_and_a_first_poll_have_both_arrived(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, bar_poll_delay_seconds=30)
    w.connect()
    w.run(20)
    assert len(w.history_requests()) == 2  # history done...
    assert w.md.bars(w.clock.now) == ()  # ...but no poll yet: nothing to decide on
    w.run(15)
    assert w.md.bars(w.clock.now)


# --- polls -------------------------------------------------------------------------------


def test_a_poll_keeps_only_bars_closed_when_it_was_sent(world: World) -> None:
    ready(world)
    bars = world.md.bars(world.clock.now)
    newest = bars[-1].ts
    sent = world.clock.now - timedelta(seconds=2)  # the poll went out ~2 steps ago
    assert newest + MINUTE <= sent
    assert newest == sent.replace(second=0) - MINUTE  # the forming minute was dropped
    assert all(b.symbol == "NQ" for b in bars)
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)


def test_bars_are_exactly_what_ibkr_sent(world: World) -> None:
    ready(world)
    bar = world.md.bars(world.clock.now)[-1]
    raw = ib_bar(DEC.con_id, bar.ts)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        raw.open,
        raw.high,
        raw.low,
        raw.close,
        raw.volume,
    )


def test_one_poll_per_minute_after_the_delay(world: World) -> None:
    ready(world)
    polls = len([a for a in world.gw.named("bars") if a[2] == ""])
    boundary = world.clock.now.replace(second=0) + MINUTE
    world.clock.now = boundary + timedelta(seconds=2)  # inside the delay: no poll yet
    world.run(2)
    assert len([a for a in world.gw.named("bars") if a[2] == ""]) == polls
    world.clock.now = boundary + timedelta(seconds=5)
    world.run(20)
    assert len([a for a in world.gw.named("bars") if a[2] == ""]) == polls + 1


def test_a_poll_covers_the_gap_since_the_newest_bar(world: World) -> None:
    ready(world)
    world.md.on_market_connection(Connection.LOST)
    world.clock.now += timedelta(minutes=40)
    before = len(world.gw.named("bars"))
    world.connect()
    world.run(70)
    polls = [a for a in world.gw.named("bars")[before:] if a[2] == ""]
    seconds = int(polls[0][3].split()[0])
    assert seconds >= 40 * 60  # the outage is asked for, not just the last few minutes
    bars = world.md.bars(world.clock.now)
    stamps = [b.ts for b in bars if b.ts.date() == world.clock.now.date()]
    assert all(b - a == MINUTE for a, b in pairwise(stamps))  # no hole


def test_a_short_poll_still_asks_for_the_minimum(world: World) -> None:
    ready(world)
    polls = [a for a in world.gw.named("bars") if a[2] == ""]
    assert int(polls[-1][3].split()[0]) >= world.execution.market_data.min_poll_seconds


def test_a_revised_bar_is_counted(world: World) -> None:
    ready(world)
    done = world.md.bars(world.clock.now)[-1]
    req = MARKET_REQUEST_BASE + 10_000
    # a later poll carries a different version of a bar already counted complete
    world.md._pending[req] = world.md._pending.get(req) or _poll_request(world)
    world.md.on_bar(req, IbBar(done.ts, done.open, done.high + 5, done.low, done.close, 99))
    world.md.on_bars_end(req)
    assert world.md.revisions == 1


def _poll_request(w: World) -> Any:
    from footprint.brokers.ibkr.market import _Kind, _Request

    return _Request(_Kind.POLL, w.clock.now, con_id=DEC.con_id)


# --- the splice ------------------------------------------------------------------------

# Friday 2026-09-11 07:00 PT: the Sep future rolled to Dec on Sep 10.
FRIDAY = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)


def test_history_across_a_roll_is_spliced_and_back_adjusted(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, now=FRIDAY)
    ready(w)
    # days: Sep 8, Sep 9 (Sep future), Sep 10 (Dec); plus Dec's bars for the splice day
    assert sorted(w.history_requests()) == sorted(
        [
            (SEP.con_id, date(2026, 9, 9), "86400 S"),
            (SEP.con_id, date(2026, 9, 10), "86400 S"),
            (DEC.con_id, date(2026, 9, 11), "86400 S"),
            (DEC.con_id, date(2026, 9, 10), "86400 S"),  # Dec on Sep 9: the splice day
        ]
    )
    bars = {b.ts: b for b in w.md.bars(w.clock.now)}
    cut = datetime(2026, 9, 9, 23, 59, tzinfo=UTC)  # the last minute both traded
    shift = price(DEC.con_id, cut) - price(SEP.con_id, cut)
    old = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    assert bars[old].close == price(SEP.con_id, old) + shift  # back-adjusted
    assert bars[cut].close == price(DEC.con_id, cut)  # the splice minute agrees
    after = cut + MINUTE
    assert bars[after].close == price(DEC.con_id, after)  # the newer future, unshifted


def test_a_splice_with_no_common_minute_gives_no_bars_and_is_retried(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, now=FRIDAY, retry_seconds=30)
    w.missing.add((DEC.con_id, date(2026, 9, 9)))  # Dec never traded on the splice day
    w.connect()
    w.run(20)
    w.clock.now = w.clock.now.replace(second=0) + MINUTE + timedelta(seconds=6)
    w.run(2)
    assert w.md.bars(w.clock.now) == ()  # never a guessed splice
    w.run(2)
    assert any("cannot splice" in a for a in w.alarms)
    asked = len(w.history_requests())
    w.missing.clear()
    w.run(40)  # after retry_seconds the empty day is asked for again
    assert len(w.history_requests()) > asked


def test_rolling_during_the_session_moves_everything_to_the_next_future(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    before_roll = datetime(2026, 12, 9, 23, 58, tzinfo=UTC)
    w = World(execution, instrument, now=before_roll)
    w.connect()
    w.run(10)
    assert w.md.front == DEC
    dec_line = w.line(DEC)
    w.clock.now = datetime(2026, 12, 10, 0, 0, 30, tzinfo=UTC)
    w.run(5)
    assert w.md.front == MAR
    assert ("cancel_quotes", (dec_line,)) in w.gw.sent
    w.line(MAR)  # the new future is streamed
    w.run(30)
    assert (MAR.con_id, date(2026, 12, 10), "86400 S") in w.history_requests()  # splice day


# --- errors -------------------------------------------------------------------------------


def test_a_day_with_no_data_is_an_empty_answer_not_a_failure(world: World) -> None:
    world.no_answer.add("bars")
    world.connect()
    world.run(1)
    req = world.gw.named("bars")[0][0]
    world.md.on_market_error(
        req, 162, "Historical Market Data Service error message:HMDS query returned no data"
    )
    world.no_answer.clear()
    world.run(20)
    assert not any("history failed" in a for a in world.alarms)
    assert len(world.history_requests()) == 2  # moved on to the next day


def test_a_history_error_is_alarmed_once_and_retried_under_a_new_id(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, retry_seconds=10)
    w.no_answer.add("bars")
    w.connect()
    w.run(1)
    first = w.gw.named("bars")[0][0]
    w.md.on_market_error(first, 162, "Historical data request pacing violation")
    w.run(5)
    assert len(w.gw.named("bars")) == 1  # waiting out retry_seconds
    w.run(10)
    second = w.gw.named("bars")[1][0]
    assert second != first
    w.md.on_market_error(second, 162, "Historical data request pacing violation")
    assert sum("history failed" in a for a in w.alarms) == 1  # once per session


def test_an_unanswered_request_is_dropped_cancelled_and_retried(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = World(execution, instrument, request_timeout_seconds=5, retry_seconds=5)
    w.no_answer.add("bars")
    w.connect()
    w.run(7)
    first = w.gw.named("bars")[0][0]
    assert ("cancel_bars", (first,)) in w.gw.sent
    assert any("no answer" in a for a in w.alarms)
    w.no_answer.clear()
    w.run(10)
    assert len(w.gw.named("bars")) >= 2


def test_late_answers_to_forgotten_requests_are_ignored(world: World) -> None:
    ready(world)
    before = world.md.bars(world.clock.now)
    alarms = list(world.alarms)
    stranger = MARKET_REQUEST_BASE + 999_999
    world.md.on_bar(stranger, ib_bar(DEC.con_id, TUESDAY))
    world.md.on_bars_end(stranger)
    world.md.on_quote(stranger, QuoteField.BID, D(1))
    world.md.on_market_error(stranger, 354, "not subscribed")
    world.md.on_contract_details_end(stranger)
    assert world.md.bars(world.clock.now) == before
    assert world.alarms == alarms


def test_a_request_that_cannot_be_sent_is_retried(world: World) -> None:
    def refuse(*args: Any) -> None:
        raise ConnectionError("IB Gateway is not connected")

    world.gw.request_contract_details = refuse  # type: ignore[assignment]
    world.connect()
    world.run(3)
    assert any("could not be sent" in a for a in world.alarms)


# --- the option chain -------------------------------------------------------------------

CHAIN = tuple(
    opt(1000 + i * 2 + (r == "P"), "20261007", 19950 + 10 * i, r)
    for i in range(30)
    for r in ("C", "P")
)


def chain_world(execution: ExecutionProfile, instrument: InstrumentProfile, **kw: Any) -> World:
    params = kw.pop(
        "params",
        [
            ("Q2C", "20", frozenset({"20261007", "20261008"}), frozenset(c.strike for c in CHAIN)),
            ("Q3C", "20", frozenset({"20261008"}), frozenset({D(20000)})),
        ],
    )
    w = World(execution, instrument, options=CHAIN, params=params, **kw)
    w.connect()
    w.run(3)
    w.tick(DEC, D("20025.00"), D("20025.50"))  # the future's mid: 20025.25
    w.run(2)
    return w


def test_the_option_chain_is_looked_up_on_the_traded_future(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    ((_req, symbol, exchange, sec_type, con_id),) = w.gw.named("params")
    assert (symbol, exchange, sec_type, con_id) == ("NQ", "CME", "FUT", DEC.con_id)
    option_lookups = [q for _r, q in w.gw.named("details") if q.sec_type == "FOP"]
    assert [(q.expiry, q.trading_class) for q in option_lookups] == [("20261007", "Q2C")]


def test_the_nearest_expiry_in_the_dte_range_is_chosen(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    params = [
        ("Q1C", "20", frozenset({"20261006", "20261009"}), frozenset({D(20000)})),  # 0 DTE
        ("Q2C", "20", frozenset({"20261008"}), frozenset({D(20000)})),
        ("Q3C", "20", frozenset({"20261010"}), frozenset({D(20000)})),  # 4 DTE
    ]
    w = World(execution, instrument, params=params)
    w.connect()
    w.run(3)
    lookups = [q for _r, q in w.gw.named("details") if q.sec_type == "FOP"]
    assert [(q.expiry, q.trading_class) for q in lookups] == [("20261008", "Q2C")]


def test_two_classes_on_one_expiry_pick_the_fuller_one_every_time(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    params = [
        ("AAA", "20", frozenset({"20261007"}), frozenset({D(20000)})),
        ("ZZZ", "20", frozenset({"20261007"}), frozenset({D(20000), D(20010)})),
        ("BAD", "2", frozenset({"20261007"}), frozenset({D(1), D(2), D(3)})),  # wrong size
    ]
    w = World(execution, instrument, params=params)
    w.connect()
    w.run(3)
    lookups = [q for _r, q in w.gw.named("details") if q.sec_type == "FOP"]
    assert [q.trading_class for q in lookups] == ["ZZZ"]


def test_no_expiry_in_range_is_alarmed_once_and_nothing_streams(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    params = [("Q2C", "20", frozenset({"20261020"}), frozenset({D(20000)}))]
    w = World(execution, instrument, params=params)
    w.connect()
    w.run(10)
    assert sum("expires in 1-3 days" in a for a in w.alarms) == 1
    assert [a for a in w.gw.named("quotes") if a[1].sec_type == "FOP"] == []


def test_strikes_inside_the_window_are_streamed_nearest_first_and_registered(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument, strike_window_points=D(75))
    streamed = [c for _r, c in w.gw.named("quotes") if c.sec_type == "FOP"]
    strikes = {c.strike for c in streamed}
    assert strikes == {D(s) for s in range(19960, 20101, 10)}  # within 75 of 20025.25
    assert len(streamed) == 2 * len(strikes)  # calls and puts
    assert abs(streamed[0].strike - D("20025.25")) <= abs(streamed[-1].strike - D("20025.25"))
    for c in streamed:
        assert w.contracts.ib(option_contract_id(c)) == c  # Diego can trade it


def test_the_line_cap_is_never_exceeded_and_is_alarmed(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument, max_quote_lines=5)
    options = [c for _r, c in w.gw.named("quotes") if c.sec_type == "FOP"]
    assert len(options) == 4  # the future holds the fifth line
    assert {c.strike for c in options} == {D(20020), D(20030)}  # the nearest
    assert sum("max_quote_lines" in a for a in w.alarms) == 1


def test_the_window_moves_only_once_the_future_has_moved_far_enough(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument, strike_window_points=D(75), recenter_points=D(25))
    count = len(w.gw.named("quotes"))
    w.tick(DEC, D("20045"), D("20045.50"))  # 20 points: inside recenter_points
    w.run(2)
    assert len(w.gw.named("quotes")) == count
    w.tick(DEC, D("20055"), D("20055.50"))  # 30 points: move the window up
    w.run(2)
    new = [c for _r, c in w.gw.named("quotes")[count:]]
    assert {c.strike for c in new} == {D(20110), D(20120), D(20130)}
    cancelled = {r for (r,) in w.gw.named("cancel_quotes")}
    gone = {
        r
        for r, c in w.gw.named("quotes")
        if c.sec_type == "FOP" and c.strike in (D(19960), D(19970), D(19980))
    }
    assert gone <= cancelled


def test_quotes_reach_the_book_only_on_the_drivers_step(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    w.tick(call, D("150.25"), D("151.75"))
    assert [q for q in w.published if q.contract_id == option_contract_id(call)] == []
    w.run(1)
    quotes = [q for q in w.published if q.contract_id == option_contract_id(call)]
    assert quotes[-1].bid == D("150.25")
    assert quotes[-1].ask == D("151.75")
    assert quotes[-1].ts <= w.clock.now


def test_the_chain_holds_only_fresh_quotes_and_is_as_old_as_its_oldest(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    put = next(c for c in CHAIN if c.strike == D(20030) and c.right == "P")
    w.tick(call, D(150), D(151))
    w.clock.now += timedelta(seconds=2)
    w.tick(put, D(140), D(141))
    w.tick(DEC, D("20025.00"), D("20025.50"))
    w.md.on_quote(w.line(call), QuoteField.OPEN_INTEREST, D(1234))
    now = w.clock.now
    chain = w.md.chain(now)
    assert chain is not None
    assert chain.underlying == "NQ"
    assert chain.underlying_price == D("20025.25")
    assert chain.ts == now - timedelta(seconds=2)  # the call's ticks
    by_id = {q.contract_id: q for q in chain.quotes}
    q = by_id[option_contract_id(call)]
    assert (q.expiry, q.strike, q.right, q.bid, q.ask) == (
        date(2026, 10, 7),
        D(20020),
        Right.CALL,
        D(150),
        D(151),
    )
    assert q.open_interest == 1234
    assert by_id[option_contract_id(put)].open_interest == 0  # unknown: fails the OI rule
    max_age = timedelta(seconds=w.execution.quotes.max_age_seconds)
    later = now + max_age - timedelta(seconds=1)
    chain = w.md.chain(later)  # the call is now too old; the put is not
    assert chain is None or option_contract_id(call) not in {x.contract_id for x in chain.quotes}


def test_no_chain_without_a_fresh_future_quote(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    w.tick(call, D(150), D(151))
    assert w.md.chain(w.clock.now) is not None
    stale = w.clock.now + timedelta(seconds=w.execution.quotes.max_age_seconds + 1)
    w.clock.now = stale
    w.tick(call, D(150), D(151))  # the option is fresh again; the future is not
    assert w.md.chain(stale) is None


def test_an_option_without_an_ask_or_crossed_is_left_out(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    c1 = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    c2 = next(c for c in CHAIN if c.strike == D(20030) and c.right == "C")
    c3 = next(c for c in CHAIN if c.strike == D(20010) and c.right == "C")
    w.tick(c1, D(150), None)  # no ask
    w.tick(c2, D(152), D(151))  # crossed
    w.tick(c3, None, D(160))  # no bid: a bid of 0
    chain = w.md.chain(w.clock.now)
    assert chain is not None
    ids = {q.contract_id: q for q in chain.quotes}
    assert set(ids) == {option_contract_id(c3)}
    assert ids[option_contract_id(c3)].bid == 0


def test_a_refused_stream_is_dropped_alarmed_once_and_asked_again_later(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument, retry_seconds=10)
    options = [(r, c) for r, c in w.gw.named("quotes") if c.sec_type == "FOP"]
    for r, _c in options:
        w.md.on_market_error(r, 354, "Requested market data is not subscribed")
    assert sum("option quote failed" in a for a in w.alarms) == 1
    count = len(w.gw.named("quotes"))
    w.tick(DEC, D("20025.00"), D("20025.50"))
    w.run(5)
    assert len(w.gw.named("quotes")) == count  # waiting out retry_seconds
    w.tick(DEC, D("20025.00"), D("20025.50"))
    w.run(6)
    again = [c for _r, c in w.gw.named("quotes")[count:]]
    assert {c.con_id for c in again} == {c.con_id for _r, c in options}  # new ids
    assert min(r for r, _ in w.gw.named("quotes")[count:]) > max(r for r, _ in options)


def test_a_refused_future_stream_means_no_chain_until_it_is_back(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument, retry_seconds=10)
    w.md.on_market_error(w.line(DEC), 10167, "Requested market data is not subscribed")
    assert w.md.chain(w.clock.now) is None
    assert any("future quote failed" in a for a in w.alarms)
    w.run(11)
    assert len([c for _r, c in w.gw.named("quotes") if c == DEC]) == 2


def test_a_lost_connection_means_no_chain_and_everything_is_asked_again(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    first = {r for r, _c in w.gw.named("quotes")}
    w.md.on_market_connection(Connection.LOST)
    assert w.md.chain(w.clock.now) is None
    w.run(3)
    assert {r for r, _c in w.gw.named("quotes")} == first  # nothing asked while down
    w.connect()
    w.run(2)
    w.tick(DEC, D("20025.00"), D("20025.50"))
    w.run(2)
    again = {r for r, _c in w.gw.named("quotes")} - first
    assert len(again) == len(first)  # every stream asked again, under new ids
    assert len(w.gw.named("params")) == 1  # the static lookups are not repeated
    assert len([q for _r, q in w.gw.named("details") if q.sec_type == "FUT"]) == 1


def test_a_reconnect_does_not_download_history_again(world: World) -> None:
    ready(world)
    loaded = len(world.history_requests())
    world.md.on_market_connection(Connection.LOST)
    world.connect()
    world.run(70)
    assert len(world.history_requests()) == loaded


def test_the_market_closed_plans_no_options(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    saturday = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
    w = World(execution, instrument, now=saturday, options=CHAIN)
    w.connect()
    w.run(5)
    assert [q for _r, q in w.gw.named("details") if q.sec_type == "FOP"] == []


def test_the_option_lookup_uses_the_configured_security_type(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    custom = replace(execution, ibkr=replace(execution.ibkr, option_security_type="XYZ"))
    w = World(custom, instrument)
    w.connect()
    w.run(3)
    assert [q.sec_type for _r, q in w.gw.named("details")][1:] == ["XYZ"]


# --- review-before-review ----------------------------------------------------------------


def test_ibkr_refusing_on_the_callers_own_thread_neither_deadlocks_nor_misleads(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    """ibapi answers a request made while not connected with an error on the calling
    thread, straight back into the service while it is mid-step."""
    w = World(execution, instrument, retry_seconds=10)

    def refuse_at_once(name: str, args: tuple[Any, ...]) -> None:
        if name == "quotes":
            w.md.on_market_error(args[0], 504, "Not connected")
        else:
            World._answer(w, name, args)

    w.gw.answer = refuse_at_once
    w.connect()
    w.run(3)  # would hang here with a plain lock
    assert w.md._underlying is None  # never left pointing at the refused stream
    assert w.md.chain(w.clock.now) is None
    assert any("future quote failed" in a for a in w.alarms)


def test_ibkr_callbacks_reach_the_service_through_the_real_binding(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    pytest.importorskip("ibapi")
    from ibapi.common import BarData, TickAttrib
    from ibapi.ticktype import TickTypeEnum

    from tests.brokers.test_ibkr_binding import ACCOUNT, bound

    binding, client, _, _ = bound()
    w = World(execution, instrument)
    md = MarketData(
        binding, w.contracts, w.published.append, instrument, w.execution, w.alarms.append, w.clock
    )
    binding.attach_market(md)
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(1)  # CONNECTED reaches the service
    md.step(w.clock.now)
    ((_, (req, query)),) = [c for c in client.calls if c[0] == "reqContractDetails"]
    assert query.secType == "FUT"
    assert query.includeExpired
    from ibapi.contract import Contract, ContractDetails

    details = ContractDetails()
    c = Contract()
    c.conId, c.symbol, c.secType, c.exchange, c.currency = 500, "NQ", "FUT", "CME", "USD"
    c.lastTradeDateOrContractMonth, c.multiplier, c.tradingClass = "20261218", "20", "NQ"
    details.contract = c
    binding._wrapper.contractDetails(req, details)
    binding._wrapper.contractDetailsEnd(req)
    md.step(w.clock.now)
    assert md.front is not None
    assert md.front.con_id == 500
    quote_req = next(args[0] for n, args in client.calls if n == "reqMktData")
    binding._wrapper.tickPrice(quote_req, TickTypeEnum.BID, 20000.25, TickAttrib())
    binding._wrapper.tickPrice(quote_req, TickTypeEnum.ASK, 20000.75, TickAttrib())
    line = md._lines[quote_req]
    assert (line.bid, line.ask) == (D("20000.25"), D("20000.75"))
    bars_req = next(args[0] for n, args in client.calls if n == "reqHistoricalData")
    b = BarData()
    b.date, b.open, b.high, b.low, b.close, b.volume = "1791158400", 1.0, 1.0, 1.0, 1.0, D(3)
    binding._wrapper.historicalData(bars_req, b)
    assert md._pending[bars_req].bars[0].volume == 3
