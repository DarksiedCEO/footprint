"""The binding's market data side: requests become ibapi calls, ticks and bars come
back as exact values, and delayed data never gets through."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

if not os.environ.get("FOOTPRINT_REQUIRE_IBAPI"):
    pytest.importorskip("ibapi", reason="ibapi not installed: scripts/install_ibapi.py")

from ibapi.common import BarData, TickAttrib
from ibapi.contract import Contract, ContractDetails
from ibapi.ticktype import TickTypeEnum

from footprint.brokers.ibkr.gateway import (
    MARKET_REQUEST_BASE,
    ContractQuery,
    IbBar,
    IbContract,
    QuoteField,
)
from tests.brokers.test_ibkr_binding import ACCOUNT, Events, bound, opened

D = Decimal
REQ = MARKET_REQUEST_BASE + 7
NQ_FUT = ContractQuery("NQ", "FUT", "CME", "USD", expiry="202612")
CALL = IbContract(601, "NQ", "FOP", "CME", "USD", "20261007", D(25000), "C", "20", "Q2C")


def with_market(**changes: Any) -> tuple[Any, Any, Events, Events]:
    binding, client, events, _ = opened(**changes)
    market = Events()
    binding.attach_market(market)
    return binding, client, events, market


# --- what goes out ---------------------------------------------------------------------


def test_a_session_asks_for_real_time_data_before_it_opens() -> None:
    binding, client, events, _ = bound()
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(1)
    names = [name for name, _ in client.calls]
    assert ("reqMarketDataType", (1,)) in client.calls  # real-time, never delayed (3)
    assert names.index("reqMarketDataType") == 0
    assert events.got[-1][0] == "on_connection"


def test_a_contract_lookup_goes_out_with_its_fields() -> None:
    binding, client, _, _ = with_market()
    binding.request_contract_details(REQ, NQ_FUT)
    ((name, (req, contract)),) = client.calls
    assert (name, req) == ("reqContractDetails", REQ)
    assert (contract.symbol, contract.secType, contract.exchange, contract.currency) == (
        "NQ",
        "FUT",
        "CME",
        "USD",
    )
    assert contract.lastTradeDateOrContractMonth == "202612"
    assert contract.conId == 0


def test_an_option_lookup_carries_strike_right_and_class() -> None:
    binding, client, _, _ = with_market()
    query = ContractQuery(
        "NQ", "FOP", "CME", "USD", "20261007", D("25000.25"), "C", "Q2C", con_id=0
    )
    binding.request_contract_details(REQ, query)
    contract = client.calls[0][1][1]
    assert (contract.strike, contract.right, contract.tradingClass) == (25000.25, "C", "Q2C")


def test_option_params_quotes_and_bars_use_ibapi_10_50_calls() -> None:
    binding, client, _, _ = with_market()
    binding.request_option_params(REQ, "NQ", "FUT", 555)
    binding.request_quotes(REQ + 1, CALL)
    binding.cancel_quotes(REQ + 1)
    binding.request_bars(REQ + 2, NQ_FUT, "", "1 D", True)
    binding.cancel_bars(REQ + 2)
    assert client.calls[0] == ("reqSecDefOptParams", (REQ, "NQ", "", "FUT", 555))
    name, (req, contract, generic, snapshot, regulatory, options) = client.calls[1]
    assert (name, req, contract.conId, contract.tradingClass) == ("reqMktData", REQ + 1, 601, "Q2C")
    assert (generic, snapshot, regulatory, options) == ("101", False, False, [])  # 101: OI
    assert client.calls[2] == ("cancelMktData", (REQ + 1,))
    name, args = client.calls[3]
    assert name == "reqHistoricalData"
    assert args[0] == REQ + 2
    assert args[2:] == ("", "1 D", "1 min", "TRADES", 0, 2, True, [])  # all hours, epoch
    assert client.calls[4] == ("cancelHistoricalData", (REQ + 2,))


# --- what comes in -------------------------------------------------------------------------


def details(con_id: int, sec_type: str, strike: float, right: str, expiry: str) -> ContractDetails:
    d = ContractDetails()
    c = Contract()
    c.conId, c.symbol, c.secType, c.exchange, c.currency = con_id, "NQ", sec_type, "CME", "USD"
    c.lastTradeDateOrContractMonth, c.strike, c.right = expiry, strike, right
    c.multiplier, c.tradingClass = "20", "Q2C" if sec_type == "FOP" else "NQ"
    d.contract = c
    return d


def test_contract_details_arrive_as_ib_contracts() -> None:
    binding, _, _, market = with_market()
    binding._wrapper.contractDetails(REQ, details(601, "FOP", 25000.25, "C", "20261007"))
    binding._wrapper.contractDetails(REQ, details(555, "FUT", 0.0, "", "20261218"))
    binding._wrapper.contractDetailsEnd(REQ)
    assert market.got == [
        (
            "on_contract_details",
            (
                REQ,
                IbContract(
                    601, "NQ", "FOP", "CME", "USD", "20261007", D("25000.25"), "C", "20", "Q2C"
                ),
            ),
        ),
        (
            "on_contract_details",
            (REQ, IbContract(555, "NQ", "FUT", "CME", "USD", "20261218", D(0), "", "20", "NQ")),
        ),
        ("on_contract_details_end", (REQ,)),
    ]


def test_option_params_arrive_as_exact_sets() -> None:
    binding, _, _, market = with_market()
    binding._wrapper.securityDefinitionOptionParameter(
        REQ, "CME", 555, "Q2C", "20", {"20261007", "20261008"}, {24975.0, 25000.0, 25012.5}
    )
    binding._wrapper.securityDefinitionOptionParameterEnd(REQ)
    assert market.got == [
        (
            "on_option_params",
            (
                REQ,
                "CME",
                "Q2C",
                "20",
                frozenset({"20261007", "20261008"}),
                frozenset({D(24975), D(25000), D("25012.5")}),
            ),
        ),
        ("on_option_params_end", (REQ,)),
    ]


def test_real_time_bid_ask_and_open_interest_arrive_exactly() -> None:
    binding, _, _, market = with_market()
    w = binding._wrapper
    w.tickPrice(REQ, TickTypeEnum.BID, 151.25, TickAttrib())
    w.tickPrice(REQ, TickTypeEnum.ASK, 151.75, TickAttrib())
    w.tickPrice(REQ, TickTypeEnum.BID, -1.0, TickAttrib())  # IBKR: no bid now
    w.tickSize(REQ, TickTypeEnum.OPTION_CALL_OPEN_INTEREST, D(1234))
    w.tickSize(REQ, TickTypeEnum.OPTION_PUT_OPEN_INTEREST, D(99))
    assert market.got == [
        ("on_quote", (REQ, QuoteField.BID, D("151.25"))),
        ("on_quote", (REQ, QuoteField.ASK, D("151.75"))),
        ("on_quote", (REQ, QuoteField.BID, None)),
        ("on_quote", (REQ, QuoteField.OPEN_INTEREST, D(1234))),
        ("on_quote", (REQ, QuoteField.OPEN_INTEREST, D(99))),
    ]


def test_delayed_and_other_ticks_never_get_through() -> None:
    binding, _, _, market = with_market()
    w = binding._wrapper
    w.tickPrice(REQ, TickTypeEnum.DELAYED_BID, 150.0, TickAttrib())
    w.tickPrice(REQ, TickTypeEnum.DELAYED_ASK, 152.0, TickAttrib())
    w.tickPrice(REQ, TickTypeEnum.LAST, 151.0, TickAttrib())
    w.tickSize(REQ, TickTypeEnum.BID_SIZE, D(5))
    w.tickSize(REQ, TickTypeEnum.DELAYED_BID_SIZE, D(5))
    assert market.got == []


def bar(epoch: int, o: float, h: float, low: float, c: float, volume: Any) -> BarData:
    b = BarData()
    b.date, b.open, b.high, b.low, b.close, b.volume = str(epoch), o, h, low, c, volume
    return b


def test_bars_arrive_in_utc_with_exact_prices() -> None:
    binding, _, _, market = with_market()
    w = binding._wrapper
    epoch = int(datetime(2026, 10, 5, 14, 0, tzinfo=UTC).timestamp())
    w.historicalData(REQ, bar(epoch, 25000.25, 25010.0, 24990.5, 25005.75, D(1234)))
    w.historicalDataEnd(REQ, "", "")
    w.historicalDataUpdate(REQ, bar(epoch + 60, 25005.75, 25006.0, 25001.0, 25002.0, D(17)))
    t = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    assert market.got == [
        ("on_bar", (REQ, IbBar(t, D("25000.25"), D(25010), D("24990.5"), D("25005.75"), 1234))),
        ("on_bars_end", (REQ,)),
        (
            "on_bar_update",
            (REQ, IbBar(t.replace(minute=1), D("25005.75"), D(25006), D(25001), D(25002), 17)),
        ),
    ]
    assert market.got[0][1][1].ts.tzinfo is UTC


def test_an_unreadable_bar_drops_the_connection_rather_than_guess() -> None:
    binding, client, _, market = with_market()
    binding._wrapper.historicalData(REQ, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D("0.5")))
    assert market.got == []
    assert not client.connected


def test_a_bar_with_an_unreadable_date_drops_the_connection() -> None:
    binding, client, _, market = with_market()
    b = bar(0, 1.0, 1.0, 1.0, 1.0, D(1))
    b.date = "20261005 14:00:00 US/Eastern"  # not the epoch format asked for
    binding._wrapper.historicalData(REQ, b)
    assert market.got == []
    assert not client.connected


def test_market_errors_go_to_the_market_and_order_errors_to_the_adapter() -> None:
    binding, _, events, market = with_market()
    binding._wrapper.error(REQ, 0, 354, "Requested market data is not subscribed")
    binding._wrapper.error(7, 0, 201, "Order rejected")
    binding._wrapper.error(-1, 0, 1100, "Connectivity lost")
    assert market.got == [
        ("on_market_error", (REQ, 354, "Requested market data is not subscribed"))
    ]
    assert events.got == [
        ("on_error", (7, 201, "Order rejected")),
        ("on_error", (-1, 1100, "Connectivity lost")),
    ]


def test_market_callbacks_without_a_market_attached_are_dropped() -> None:
    binding, client, events, _ = opened()  # the read-only check attaches no market
    binding._wrapper.tickPrice(REQ, TickTypeEnum.BID, 1.0, TickAttrib())
    binding._wrapper.error(REQ, 0, 354, "not subscribed")
    assert events.got == []
    assert client.connected


def test_an_unreadable_option_strike_drops_the_connection_rather_than_guess() -> None:
    binding, client, _, market = with_market()
    binding._wrapper.securityDefinitionOptionParameter(
        REQ, "CME", 555, "Q2C", "20", {"20261007"}, {25000.0, float("nan")}
    )
    assert market.got == []
    assert not client.connected


def test_an_unset_open_interest_drops_the_connection_rather_than_guess() -> None:
    from ibapi.common import UNSET_DECIMAL

    binding, client, _, market = with_market()
    binding._wrapper.tickSize(REQ, TickTypeEnum.OPTION_CALL_OPEN_INTEREST, UNSET_DECIMAL)
    assert market.got == []
    assert not client.connected


def test_a_zero_bid_reads_as_no_bid() -> None:
    binding, _, _, market = with_market()
    binding._wrapper.tickPrice(REQ, TickTypeEnum.BID, 0.0, TickAttrib())
    assert market.got == [("on_quote", (REQ, QuoteField.BID, None))]


def test_the_request_id_boundary_is_exact() -> None:
    binding, _, events, market = with_market()
    binding._wrapper.error(MARKET_REQUEST_BASE, 0, 354, "not subscribed")
    binding._wrapper.error(MARKET_REQUEST_BASE - 1, 0, 201, "rejected")
    assert market.got == [("on_market_error", (MARKET_REQUEST_BASE, 354, "not subscribed"))]
    assert events.got == [("on_error", (MARKET_REQUEST_BASE - 1, 201, "rejected"))]


def test_a_bar_with_a_zero_price_drops_the_connection() -> None:
    binding, client, _, market = with_market()
    binding._wrapper.historicalData(REQ, bar(1759672800, 1.0, 1.0, 0.0, 1.0, D(1)))
    assert market.got == []
    assert not client.connected
