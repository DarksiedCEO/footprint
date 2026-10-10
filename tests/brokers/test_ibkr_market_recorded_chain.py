"""Q3 slice 2: what the option-chain recorder reads from MarketData. The same options the
runner's `chain()` offers, but each with its own quote time and the open interest IBKR
actually sent (None when it sent none, never 0), plus the future's own quote. `chain()`
itself is unchanged (its tests are in test_ibkr_market_service.py). Hand-fed quotes;
nothing here says anything about prices."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from footprint.brokers.ibkr.gateway import Connection, QuoteField
from footprint.brokers.ibkr.market import option_contract_id
from footprint.core.config import ExecutionProfile, InstrumentProfile
from footprint.core.options import Right
from tests.brokers.test_ibkr_market_service import CHAIN, DEC, World, chain_world

D = Decimal


def _two_options(w: World) -> tuple[str, str]:
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    put = next(c for c in CHAIN if c.strike == D(20030) and c.right == "P")
    w.tick(call, D(150), D(151))
    w.clock.now += timedelta(seconds=2)
    w.tick(put, D(140), D(141))
    w.tick(DEC, D("20025.00"), D("20025.50"))
    w.md.on_quote(w.line(call), QuoteField.OPEN_INTEREST, D(1234))
    return option_contract_id(call), option_contract_id(put)


def test_each_option_carries_its_own_quote_time_and_the_open_interest_ibkr_sent(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call_id, put_id = _two_options(w)
    now = w.clock.now
    rec = w.md.recorded_chain(now)
    assert rec is not None
    assert (rec.future.bid, rec.future.ask, rec.future.ts) == (
        D("20025.00"),
        D("20025.50"),
        now,
    )
    by_id = {o.quote.contract_id: o for o in rec.options}
    call, put = by_id[call_id], by_id[put_id]
    assert (call.expiry, call.strike, call.right) == (date(2026, 10, 7), D(20020), Right.CALL)
    assert (call.quote.bid, call.quote.ask, call.quote.ts) == (
        D(150),
        D(151),
        now - timedelta(seconds=2),
    )
    assert (put.quote.bid, put.quote.ask, put.quote.ts) == (D(140), D(141), now)
    assert put.right is Right.PUT
    assert call.open_interest == 1234
    assert put.open_interest is None  # IBKR sent none: never written as 0


def test_open_interest_of_zero_stays_zero(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call_id, _ = _two_options(w)
    call = next(c for c in CHAIN if option_contract_id(c) == call_id)
    w.md.on_quote(w.line(call), QuoteField.OPEN_INTEREST, D(0))
    rec = w.md.recorded_chain(w.clock.now)
    assert rec is not None
    assert {o.quote.contract_id: o.open_interest for o in rec.options}[call_id] == 0


def test_it_offers_exactly_the_options_chain_offers(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    _two_options(w)
    c3 = next(c for c in CHAIN if c.strike == D(20010) and c.right == "C")
    c4 = next(c for c in CHAIN if c.strike == D(20040) and c.right == "C")
    w.tick(c3, None, D(160))  # no bid: a bid of 0, still a quote
    w.tick(c4, D(150), None)  # no ask: not a quote
    for later in (timedelta(0), timedelta(seconds=w.execution.quotes.max_age_seconds - 1)):
        now = w.clock.now + later
        chain, rec = w.md.chain(now), w.md.recorded_chain(now)
        assert chain is not None
        assert rec is not None
        assert [(q.contract_id, q.bid, q.ask) for q in chain.quotes] == [
            (o.quote.contract_id, o.quote.bid, o.quote.ask) for o in rec.options
        ]
        assert option_contract_id(c4) not in {o.quote.contract_id for o in rec.options}


def test_a_stale_option_is_left_out(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    _, put_id = _two_options(w)
    max_age = timedelta(seconds=w.execution.quotes.max_age_seconds)
    later = w.clock.now + max_age - timedelta(seconds=1)  # the call is 1 s too old
    w.clock.now = later
    w.tick(DEC, D("20025.00"), D("20025.50"))
    rec = w.md.recorded_chain(later)
    assert rec is not None
    assert [o.quote.contract_id for o in rec.options] == [put_id]


def test_nothing_without_a_fresh_future_quote(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    w.tick(call, D(150), D(151))
    assert w.md.recorded_chain(w.clock.now) is not None
    stale = w.clock.now + timedelta(seconds=w.execution.quotes.max_age_seconds + 1)
    w.clock.now = stale
    w.tick(call, D(150), D(151))  # the option is fresh again; the future is not
    assert w.md.recorded_chain(stale) is None


def test_nothing_without_a_future_bid(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    w.tick(call, D(150), D(151))
    w.tick(DEC, None, D("20025.50"))
    assert w.md.recorded_chain(w.clock.now) is None


def test_nothing_without_a_fresh_option(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    assert w.md.recorded_chain(w.clock.now) is None


def test_nothing_while_disconnected(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    _two_options(w)
    assert w.md.recorded_chain(w.clock.now) is not None
    w.md.on_market_connection(Connection.LOST)
    assert w.md.recorded_chain(w.clock.now) is None
