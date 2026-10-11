"""Q1: the market-data probe behind `check --market-data`. It reads quotes through a
gateway that can send no order, prints what IBKR sent (or says exactly what is
missing), and exits non-zero when quotes are missing. Nothing here needs ibapi."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from footprint.brokers.ibkr.market import option_contract_id
from footprint.brokers.ibkr.readonly import ReadOnlyGateway, probe_market
from footprint.core.config import ExecutionProfile, InstrumentProfile, load_execution
from footprint.core.options import OptionChain, OptionQuote, Right
from footprint.execution.orders import Quote
from tests.brokers.test_ibkr_market_service import CHAIN, DEC, World, chain_world
from tests.conftest import REPO_ROOT

D = Decimal
T0 = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
SEC = timedelta(seconds=1)


# --- the read-only gateway ----------------------------------------------------------------


@dataclass
class Inner:
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args: Any) -> None:
            self.calls.append((name, args))

        return record


def test_the_read_only_gateway_refuses_every_order_change() -> None:
    inner = Inner()
    gw = ReadOnlyGateway(inner)
    with pytest.raises(PermissionError):
        gw.place_order(object())  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        gw.cancel_order(7)
    with pytest.raises(PermissionError):
        gw.global_cancel()
    assert inner.calls == []  # nothing reached IBKR


def test_the_read_only_gateway_forwards_reads_and_market_requests_unchanged() -> None:
    inner = Inner()
    gw = ReadOnlyGateway(inner)
    c = object()
    gw.request_open_orders()
    gw.request_executions()
    gw.request_positions()
    gw.request_contract_details(9001, c)  # type: ignore[arg-type]
    gw.request_option_params(9002, "S", "X", "FUT", 42)
    gw.request_quotes(9003, c)  # type: ignore[arg-type]
    gw.cancel_quotes(9003)
    gw.request_bars(9004, c, "", "60 S", False)  # type: ignore[arg-type]
    gw.cancel_bars(9004)
    assert inner.calls == [
        ("request_open_orders", ()),
        ("request_executions", ()),
        ("request_positions", ()),
        ("request_contract_details", (9001, c)),
        ("request_option_params", (9002, "S", "X", "FUT", 42)),
        ("request_quotes", (9003, c)),
        ("cancel_quotes", (9003,)),
        ("request_bars", (9004, c, "", "60 S", False)),
        ("cancel_bars", (9004,)),
    ]


def test_the_read_only_gateway_has_no_other_way_out() -> None:
    """Every public method is listed: a new one must be added here, on purpose."""
    public = {n for n in dir(ReadOnlyGateway) if not n.startswith("_")}
    assert public == {
        "place_order",
        "cancel_order",
        "global_cancel",
        "request_open_orders",
        "request_executions",
        "request_positions",
        "request_contract_details",
        "request_option_params",
        "request_quotes",
        "cancel_quotes",
        "request_bars",
        "cancel_bars",
    }


# --- MarketData's two new read accessors ----------------------------------------------------


def test_the_future_quote_is_offered_only_while_fresh(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    w.tick(DEC, D("20025.00"), D("20025.50"))
    now = w.clock.now
    q = w.md.underlying_quote(now)
    assert q is not None
    assert (q.bid, q.ask, q.ts) == (D("20025.00"), D("20025.50"), now)
    max_age = timedelta(seconds=w.execution.quotes.max_age_seconds)
    assert w.md.underlying_quote(now + max_age) is not None  # exactly the limit: fresh
    assert w.md.underlying_quote(now + max_age + SEC) is None
    assert w.md.underlying_quote(now - SEC) is None  # stamped after "now": not offered


def test_no_future_quote_before_one_arrives_or_after_a_disconnect(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    from footprint.brokers.ibkr.gateway import Connection

    w = World(execution, instrument)
    w.connect()
    w.run(3)
    assert w.md.underlying_quote(w.clock.now) is None  # streaming, but nothing has arrived
    w.tick(DEC, D(20025), D("20025.5"))
    assert w.md.underlying_quote(w.clock.now) is not None
    w.md.on_market_connection(Connection.LOST)
    assert w.md.underlying_quote(w.clock.now) is None


def test_an_option_quote_is_read_by_contract_id_only_while_fresh(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    cid = option_contract_id(call)
    assert w.md.option_quote(cid, w.clock.now) is None
    w.tick(call, D(150), D(151))
    now = w.clock.now
    q = w.md.option_quote(cid, now)
    assert q is not None
    assert (q.contract_id, q.bid, q.ask, q.ts) == (cid, D(150), D(151), now)
    max_age = timedelta(seconds=w.execution.quotes.max_age_seconds)
    assert w.md.option_quote(cid, now + max_age + SEC) is None
    assert w.md.option_quote("no such contract", now) is None
    # the future's line is never returned as an option
    w.tick(DEC, D(20025), D("20025.5"))
    assert w.md.option_quote(f"{DEC.symbol} {DEC.expiry} future", now) is None


# --- the probe loop -------------------------------------------------------------------------


def oq(cid: str, strike: int, right: Right, bid: str, ask: str) -> OptionQuote:
    return OptionQuote(cid, date(2026, 10, 15), D(strike), right, D(bid), D(ask), 0)


@dataclass
class Source:
    """A scripted MarketData: from step `from_step` on, it has the quotes it was given."""

    clock: list[datetime]
    future: Quote | None = None
    options: tuple[OptionQuote, ...] = ()
    stamps: dict[str, datetime] = field(default_factory=dict)
    from_step: int = 0
    options_from_step: int = 0
    stale_now: set[str] = field(default_factory=set)
    steps: list[datetime] = field(default_factory=list)

    def step(self, now: datetime) -> None:
        self.steps.append(now)

    def _on(self) -> bool:
        return len(self.steps) > self.from_step

    def underlying_quote(self, now: datetime) -> Quote | None:
        return self.future if self._on() else None

    def chain(self, now: datetime) -> OptionChain | None:
        if not self._on() or self.future is None or not self.options:
            return None
        if len(self.steps) <= self.options_from_step:
            return None  # the future is quoted; the options are not yet
        return OptionChain("NQ", now, (self.future.bid + self.future.ask) / 2, self.options)

    def option_quote(self, contract_id: str, now: datetime) -> Quote | None:
        if not self._on() or contract_id in self.stale_now:
            return None
        for q in self.options:
            if q.contract_id == contract_id:
                return Quote(contract_id, q.bid, q.ask, self.stamps.get(contract_id, now))
        return None


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


FUTURE = Quote("NQ 20261218 future", D("20000.00"), D("20000.50"), T0 - 2 * SEC)
OPTIONS = (
    oq("c20000", 20000, Right.CALL, "150", "151"),
    oq("p20000", 20000, Right.PUT, "140", "141"),
    oq("c20025", 20025, Right.CALL, "130", "131"),
    oq("p19975", 19975, Right.PUT, "120", "121"),
    oq("c20100", 20100, Right.CALL, "60", "61"),
)


def run(
    source: Source, clock: Clock, wait: float = 10, count: int = 3, poll: float = 1
) -> tuple[int, str]:
    out = io.StringIO()

    def say(m: str) -> None:
        print(m, file=out)

    code = probe_market(source, wait, count, poll, say, clock, clock.sleep)
    return code, out.getvalue()


def test_quotes_are_printed_nearest_strike_first_and_the_probe_exits_zero() -> None:
    clock = Clock()
    src = Source([], FUTURE, OPTIONS, {"c20000": T0 - 3 * SEC}, from_step=2)
    code, text = run(src, clock, count=3)
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "future NQ 20261218 future: bid 20000.00 ask 20000.50, quote age 4.0 s"
    # mid 20000.25: 20000 CALL, 20000 PUT (distance 0.25, call first), then 20025 CALL
    assert lines[1] == "option quotes (3 of 5 fresh, nearest the future first):"
    assert lines[2] == "  2026-10-15 20000 CALL c20000: bid 150 ask 151, quote age 5.0 s"
    assert lines[3] == "  2026-10-15 20000 PUT p20000: bid 140 ask 141, quote age 0.0 s"
    assert lines[4] == "  2026-10-15 20025 CALL c20025: bid 130 ask 131, quote age 0.0 s"
    assert len(lines) == 5
    assert len(src.steps) == 3  # stopped on the first step that had quotes
    assert clock.slept == [1, 1]


def test_a_future_quote_alone_does_not_end_the_wait() -> None:
    clock = Clock()
    src = Source([], FUTURE, OPTIONS, options_from_step=3)
    code, text = run(src, clock, wait=10, count=1)
    assert code == 0
    assert len(src.steps) == 4  # the future was quoted from the first look; options on the 4th
    assert "NO MARKET DATA" not in text
    assert "  2026-10-15 20000 CALL c20000: bid 150 ask 151" in text


def test_fewer_quotes_than_asked_for_prints_them_all() -> None:
    clock = Clock()
    code, text = run(Source([], FUTURE, OPTIONS[:2]), clock, count=3)
    assert code == 0
    assert "option quotes (2 of 2 fresh" in text


def test_a_quote_gone_stale_since_the_chain_is_said_and_never_printed_as_fresh() -> None:
    clock = Clock()
    src = Source([], FUTURE, OPTIONS, stale_now={"c20000"})
    code, text = run(src, clock, count=2)
    assert code == 0
    assert "  2026-10-15 20000 CALL c20000: no fresh quote now" in text
    assert "bid 150" not in text


def test_nothing_by_the_deadline_exits_one_and_names_what_is_missing() -> None:
    clock = Clock()
    src = Source([], FUTURE, OPTIONS, from_step=10_000)
    code, text = run(src, clock, wait=5, poll=1)
    assert code == 1
    assert text.splitlines()[-1] == (
        "NO MARKET DATA after 5 s: no fresh future quote; no fresh option quote "
        "(IBKR's reason, if it sent one, is in the ALARM lines above)"
    )
    assert src.steps[0] == T0
    assert src.steps[-1] == T0 + 5 * SEC  # a last look at the deadline
    assert len(src.steps) == 6
    assert clock.slept == [1] * 5


def test_a_future_quote_without_option_quotes_exits_one() -> None:
    clock = Clock()
    code, text = run(Source([], FUTURE, ()), clock, wait=2)
    assert code == 1
    assert "future NQ 20261218 future: bid 20000.00" in text
    assert text.splitlines()[-1].startswith("NO MARKET DATA after 2 s: no fresh option quote (")


def test_a_wait_of_zero_still_looks_once() -> None:
    clock = Clock()
    src = Source([], FUTURE, OPTIONS)
    code, _ = run(src, clock, wait=0)
    assert code == 0
    assert len(src.steps) == 1
    assert clock.slept == []


def test_the_last_sleep_never_runs_past_the_deadline() -> None:
    clock = Clock()
    src = Source([], FUTURE, OPTIONS, from_step=10_000)
    code, _ = run(src, clock, wait=2.5, poll=1)
    assert code == 1
    assert clock.slept == [1, 1, 0.5]
    assert clock.now == T0 + timedelta(seconds=2.5)


# --- config -----------------------------------------------------------------------------


def test_the_probe_numbers_live_in_config() -> None:
    m = load_execution(REPO_ROOT / "config" / "execution.yaml").market_data
    assert m.probe_wait_seconds >= 1
    assert m.probe_option_quotes >= 1


@pytest.mark.parametrize("key", ["probe_wait_seconds", "probe_option_quotes"])
def test_a_probe_number_below_one_is_refused(tmp_path: Any, key: str) -> None:
    from footprint.core.config import ConfigError

    text = (REPO_ROOT / "config" / "execution.yaml").read_text()
    lines = [
        f"  {key}: 0" if line.strip().startswith(f"{key}:") else line for line in text.splitlines()
    ]
    path = tmp_path / "execution.yaml"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ConfigError, match=key):
        load_execution(path)


# --- G27-2: a missing bid is said, never printed as a price of 0 ---------------------------


ONE_SIDED = Quote("NQ 20261218 future", D(0), D("20000.50"), T0 - 2 * SEC)


def test_a_future_without_a_bid_is_named_as_the_cause_and_never_printed_as_bid_0() -> None:
    # MarketData: no bid tick -> Quote.bid == 0, and chain() is None while the future's
    # bid is not above 0, whatever the options are doing.
    clock = Clock()
    code, text = run(Source([], ONE_SIDED, ()), clock, wait=2)
    assert code == 1
    assert "future NQ 20261218 future: bid none ask 20000.50, quote age" in text
    assert "bid 0" not in text
    assert text.splitlines()[-1] == (
        "NO MARKET DATA after 2 s: the future quote has no bid (the option chain needs a "
        "two-sided future quote) (IBKR's reason, if it sent one, is in the ALARM lines above)"
    )


def test_an_option_without_a_bid_is_printed_as_bid_none() -> None:
    clock = Clock()
    no_bid = (oq("c20000", 20000, Right.CALL, "0", "2"),)
    code, text = run(Source([], FUTURE, no_bid), clock, count=1)
    assert code == 0
    assert "  2026-10-15 20000 CALL c20000: bid none ask 2, quote age" in text
    assert "bid 0 " not in text
