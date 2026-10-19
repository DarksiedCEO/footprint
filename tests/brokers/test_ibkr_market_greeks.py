"""PR 35 (greeks-quotes-v1): MarketData keeps IBKR's model Greeks per streamed option.

`option_greeks(contract_id, now)` gives the newest checked reading while it is younger
than `quotes.greeks_max_age_seconds`, or None: never a stale, impossible, future-stamped
or disconnected one. An impossible reading clears the one before it (fail closed: the
old reading is no longer the market's). The future's line carries no Greeks. Hand-fed
readings; nothing here says anything about real Greeks."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.brokers.ibkr.gateway import Connection, IbGreeks
from footprint.brokers.ibkr.market import option_contract_id
from footprint.core.config import ExecutionProfile, InstrumentProfile
from footprint.core.options import OptionGreeks, Right
from tests.brokers.test_ibkr_market_service import CHAIN, DEC, World, chain_world

D = Decimal
READING = IbGreeks(
    implied_vol=D("0.1834"),
    delta=D("0.512"),
    gamma=D("0.0009"),
    vega=D("6.1"),
    theta=D("-11.4"),
    underlying_price=D("20025.25"),
)
PUT_READING = IbGreeks(D("0.19"), D("-0.47"), D("0.0009"), D("6.0"), D("-11.0"), D("20025.25"))


def _call(w: World) -> tuple[int, str]:
    call = next(c for c in CHAIN if c.strike == D(20020) and c.right == "C")
    w.tick(call, D(150), D(151))
    return w.line(call), option_contract_id(call)


def _put(w: World) -> tuple[int, str]:
    put = next(c for c in CHAIN if c.strike == D(20030) and c.right == "P")
    w.tick(put, D(140), D(141))
    return w.line(put), option_contract_id(put)


def test_a_reading_is_kept_checked_and_stamped_on_arrival(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    stamp = w.clock.now
    g = w.md.option_greeks(cid, stamp)
    assert g == OptionGreeks(
        Right.CALL,
        D("0.1834"),
        D("0.512"),
        D("0.0009"),
        D("6.1"),
        D("-11.4"),
        D("20025.25"),
        stamp,
    )


def test_a_put_reading_is_checked_as_a_put(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _put(w)
    w.md.on_greeks(req, PUT_READING)
    g = w.md.option_greeks(cid, w.clock.now)
    assert g is not None
    assert (g.right, g.delta) == (Right.PUT, D("-0.47"))
    w.md.on_greeks(req, READING)  # a call's delta on a put: impossible
    assert w.md.option_greeks(cid, w.clock.now) is None


def test_a_reading_is_fresh_up_to_its_age_limit_and_no_further(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    stamp = w.clock.now
    limit = timedelta(seconds=execution.quotes.greeks_max_age_seconds)
    assert w.md.option_greeks(cid, stamp + limit) is not None
    assert w.md.option_greeks(cid, stamp + limit + timedelta(microseconds=1)) is None


def test_a_reading_from_the_future_is_not_offered(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    assert w.md.option_greeks(cid, w.clock.now - timedelta(microseconds=1)) is None


def test_greeks_age_on_their_own_clock_not_the_quotes(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    w.clock.now += timedelta(seconds=execution.quotes.greeks_max_age_seconds + 1)
    call = next(c for c in CHAIN if option_contract_id(c) == cid)
    w.tick(call, D(152), D(153))  # a fresh quote does not refresh old Greeks
    assert w.md.option_quote(cid, w.clock.now) is not None
    assert w.md.option_greeks(cid, w.clock.now) is None


def test_an_impossible_reading_clears_the_good_one_before_it_and_is_counted(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    assert w.md.greeks_rejected == 0
    bad = IbGreeks(D("0.18"), D("0.5"), D("-0.001"), D("6"), D("-11"), D("20025"))
    w.md.on_greeks(req, bad)
    assert w.md.option_greeks(cid, w.clock.now) is None
    assert w.md.greeks_rejected == 1
    w.md.on_greeks(req, READING)  # a good one again is offered again
    assert w.md.option_greeks(cid, w.clock.now) is not None
    assert w.md.greeks_rejected == 1


def test_a_reading_with_nothing_computed_clears_but_is_not_counted_as_rejected(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    w.md.on_greeks(req, IbGreeks(None, None, None, None, None, None))
    assert w.md.option_greeks(cid, w.clock.now) is None
    assert (w.md.greeks_rejected, w.md.greeks_incomplete) == (0, 0)


def test_the_future_never_carries_greeks(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    w.md.on_greeks(w.line(DEC), READING)
    assert w.md.option_greeks(option_contract_id(DEC), w.clock.now) is None
    assert w.md.greeks_rejected == 0


def test_a_late_reading_for_a_cancelled_or_unknown_request_is_dropped(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    w.md.on_greeks(999_999, READING)  # nothing raises, nothing kept
    assert w.md.greeks_rejected == 0


def test_nothing_while_disconnected_and_a_new_session_forgets_old_readings(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    w.md.on_market_connection(Connection.LOST)
    assert w.md.option_greeks(cid, w.clock.now) is None
    w.md.on_market_connection(Connection.CONNECTED)
    assert w.md.option_greeks(cid, w.clock.now) is None


def test_an_unknown_contract_has_no_greeks(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    assert w.md.option_greeks("NQ-no-such-contract", w.clock.now) is None


def test_an_incomplete_reading_clears_the_good_one_and_is_counted_as_incomplete(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    """Only a complete reading is usable: an agent must never find a None Greek."""
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    w.md.on_greeks(req, replace(READING, gamma=None))
    assert w.md.option_greeks(cid, w.clock.now) is None
    assert (w.md.greeks_incomplete, w.md.greeks_rejected) == (1, 0)


def test_an_incomplete_reading_with_an_impossible_value_is_incomplete(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, _ = _call(w)
    w.md.on_greeks(req, replace(READING, implied_vol=None, delta=D("1.5")))
    assert (w.md.greeks_incomplete, w.md.greeks_rejected) == (1, 0)


def test_an_unreadable_reading_clears_the_good_one_and_is_counted_as_rejected(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, READING)
    w.md.on_greeks(req, IbGreeks(None, None, None, None, None, None, unreadable=True))
    assert w.md.option_greeks(cid, w.clock.now) is None
    assert (w.md.greeks_rejected, w.md.greeks_incomplete) == (1, 0)


def test_an_unreadable_flag_wins_over_values(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    """A reading marked unreadable is never used, whatever values it carries."""
    w = chain_world(execution, instrument)
    req, cid = _call(w)
    w.md.on_greeks(req, replace(READING, unreadable=True))
    assert w.md.option_greeks(cid, w.clock.now) is None
    assert w.md.greeks_rejected == 1


def test_an_impossible_delta_is_rejected_whatever_the_volatility(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    w = chain_world(execution, instrument)
    req, _ = _call(w)
    w.md.on_greeks(req, replace(READING, delta=D("1.5")))
    assert (w.md.greeks_rejected, w.md.greeks_incomplete) == (1, 0)


def test_a_dropped_reading_is_logged_once_per_contract_per_session(
    execution: ExecutionProfile, instrument: InstrumentProfile, caplog: pytest.LogCaptureFixture
) -> None:
    w = chain_world(execution, instrument)
    req, _ = _call(w)
    put_req, _ = _put(w)
    with caplog.at_level(logging.WARNING, logger="footprint"):
        for _ in range(3):
            w.md.on_greeks(req, replace(READING, gamma=D(-1)))
            w.md.on_greeks(req, replace(READING, vega=None))
        w.md.on_greeks(put_req, replace(PUT_READING, gamma=D(-1)))
    dropped = [r for r in caplog.records if "Greeks for" in r.getMessage()]
    assert len(dropped) == 2  # one for the call, one for the put
    assert w.md.greeks_rejected == 4
    assert w.md.greeks_incomplete == 3
