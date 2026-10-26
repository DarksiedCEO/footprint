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
    MARKET_DATA_NOT_REAL_TIME,
    MARKET_DATA_UNREADABLE,
    MARKET_REQUEST_BASE,
    Connection,
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


def with_market_and_alarms() -> tuple[Any, Any, Events, list[str]]:
    binding, client, _, alarms = opened()
    market = Events()
    binding.attach_market(market)
    return binding, client, market, alarms


def assert_voided(market: Events, client: Any, alarms: list[str], what: str) -> None:
    """The one request is void, alarmed; the connection (and the orders) carry on."""
    assert len(market.got) == 1
    name, (req, code, message) = market.got[0]
    assert (name, req, code) == ("on_market_error", REQ, MARKET_DATA_UNREADABLE)
    assert what in message
    assert client.connected  # a market fault never drops the order side
    assert any(f"market request {REQ} is void" in a for a in alarms)


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
    binding.request_option_params(REQ, "NQ", "CME", "FUT", 555)
    binding.request_quotes(REQ + 1, CALL)
    binding.cancel_quotes(REQ + 1)
    binding.request_bars(REQ + 2, NQ_FUT, "", "1 D", True)
    binding.cancel_bars(REQ + 2)
    assert client.calls[0] == ("reqSecDefOptParams", (REQ, "NQ", "CME", "FUT", 555))
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


def test_an_unreadable_bar_voids_only_its_request() -> None:
    binding, client, market, alarms = with_market_and_alarms()
    binding._wrapper.historicalData(REQ, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D("0.5")))
    assert_voided(market, client, alarms, "bar")


def test_a_bar_with_an_unreadable_date_voids_only_its_request() -> None:
    binding, client, market, alarms = with_market_and_alarms()
    b = bar(0, 1.0, 1.0, 1.0, 1.0, D(1))
    b.date = "20261005 14:00:00 US/Eastern"  # not the epoch format asked for
    binding._wrapper.historicalDataUpdate(REQ, b)
    assert_voided(market, client, alarms, "bar")


@pytest.mark.parametrize(
    ("o", "h", "low", "c"),
    [
        (1.0, 1.0, 0.0, 1.0),  # a zero price
        (3.0, 2.0, 1.0, 1.5),  # open above high
        (1.5, 2.0, 1.0, 0.5),  # close below low
        (1.5, 1.0, 2.0, 1.5),  # low above high
    ],
)
def test_a_bar_that_is_not_a_bar_voids_only_its_request(
    o: float, h: float, low: float, c: float
) -> None:
    binding, client, market, alarms = with_market_and_alarms()
    binding._wrapper.historicalData(REQ, bar(1759672800, o, h, low, c, D(1)))
    assert_voided(market, client, alarms, "bar")


def test_a_bar_with_negative_volume_voids_only_its_request() -> None:
    binding, client, market, alarms = with_market_and_alarms()
    binding._wrapper.historicalData(REQ, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D(-1)))
    assert_voided(market, client, alarms, "volume")


def test_a_flat_bar_is_a_bar() -> None:
    binding, _, market, _ = with_market_and_alarms()
    binding._wrapper.historicalData(REQ, bar(1759672800, 2.0, 2.0, 2.0, 2.0, D(0)))
    assert market.got[0][0] == "on_bar"


def test_an_unreadable_option_strike_voids_only_its_request() -> None:
    binding, client, market, alarms = with_market_and_alarms()
    binding._wrapper.securityDefinitionOptionParameter(
        REQ, "CME", 555, "Q2C", "20", {"20261007"}, {25000.0, float("nan")}
    )
    assert_voided(market, client, alarms, "option chain")


def test_an_unset_open_interest_voids_only_its_request() -> None:
    from ibapi.common import UNSET_DECIMAL

    binding, client, market, alarms = with_market_and_alarms()
    binding._wrapper.tickSize(REQ, TickTypeEnum.OPTION_CALL_OPEN_INTEREST, UNSET_DECIMAL)
    assert_voided(market, client, alarms, "open interest")


def test_an_unreadable_contract_voids_only_its_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from footprint.brokers.ibkr import binding as module

    def unreadable(details: Any) -> Any:
        raise module.UnreadableValue("strike")

    monkeypatch.setattr(module, "from_details", unreadable)
    binding, client, market, alarms = with_market_and_alarms()
    binding._wrapper.contractDetails(REQ, details(601, "FOP", 1.0, "C", "20261007"))
    assert_voided(market, client, alarms, "contract")


def test_market_errors_go_to_the_market_and_order_errors_to_the_adapter() -> None:
    binding, _, events, market = with_market()
    binding._wrapper.error(REQ, 0, 354, "Requested market data is not subscribed")
    binding._wrapper.error(7, 0, 201, "Order rejected")
    binding._wrapper.error(-1, 0, 2103, "Market data farm connection is broken:usfuture")
    assert market.got == [
        ("on_market_error", (REQ, 354, "Requested market data is not subscribed")),
        ("on_market_notice", (2103, "Market data farm connection is broken:usfuture")),
    ]
    assert events.got == [
        ("on_error", (7, 201, "Order rejected")),
        ("on_error", (-1, 2103, "Market data farm connection is broken:usfuture")),
    ]


def test_market_callbacks_without_a_market_attached_are_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    binding, client, events, _ = opened()  # the read-only check attaches no market
    with caplog.at_level("WARNING", logger="footprint.ibkr.binding"):
        binding._wrapper.tickPrice(REQ, TickTypeEnum.BID, 1.0, TickAttrib())
        binding._wrapper.tickPrice(REQ, TickTypeEnum.ASK, 1.0, TickAttrib())
        binding._wrapper.historicalData(REQ, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D(1)))
    binding._wrapper.error(REQ, 0, 354, "not subscribed")
    binding._wrapper.error(-1, 0, 2104, "farm OK")  # the adapter still gets it
    assert events.got == [("on_error", (-1, 2104, "farm OK"))]
    assert client.connected
    dropped = [r for r in caplog.records if "no market service attached" in r.message]
    assert len(dropped) == 1  # said once, not on every tick


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


# --- review round 1 ------------------------------------------------------------------------


def test_bars_kept_up_to_date_must_end_now() -> None:
    binding, client, _, _ = with_market()
    with pytest.raises(ValueError, match="end now"):
        binding.request_bars(REQ, NQ_FUT, "20261005 14:00:00 UTC", "1 D", True)
    assert client.calls == []  # never sent
    binding.request_bars(REQ, NQ_FUT, "20261005 14:00:00 UTC", "1 D", False)  # history: fine
    assert client.calls[0][0] == "reqHistoricalData"


def test_the_market_hears_the_session_open_and_close() -> None:
    binding, client, events, _ = bound()
    market = Events()
    binding.attach_market(market)
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(1)
    assert market.got == [("on_market_connection", (Connection.CONNECTED,))]
    assert client.calls[0] == ("reqMarketDataType", (1,))  # real-time, asked for first
    binding._wrapper.closed()
    assert market.got[-1] == ("on_market_connection", (Connection.LOST,))
    assert events.got[-1] == ("on_connection", (Connection.LOST,))


def test_a_request_announced_as_frozen_or_delayed_is_silenced() -> None:
    binding, client, market, alarms = with_market_and_alarms()
    w = binding._wrapper
    w.marketDataType(REQ, 2)  # frozen: arrives on the ordinary BID/ASK ticks
    w.tickPrice(REQ, TickTypeEnum.BID, 151.25, TickAttrib())
    w.tickSize(REQ, TickTypeEnum.OPTION_CALL_OPEN_INTEREST, D(5))
    w.tickPrice(REQ + 1, TickTypeEnum.BID, 9.0, TickAttrib())  # another request: live
    assert market.got[0][0] == "on_market_error"
    assert market.got[0][1][:2] == (REQ, MARKET_DATA_NOT_REAL_TIME)
    assert market.got[1:] == [("on_quote", (REQ + 1, QuoteField.BID, D(9)))]
    assert any("not real-time" in a for a in alarms)
    assert client.connected


@pytest.mark.parametrize("kind", [2, 3, 4])
def test_every_non_real_time_type_is_refused(kind: int) -> None:
    binding, _, market, _ = with_market_and_alarms()
    binding._wrapper.marketDataType(REQ, kind)
    binding._wrapper.tickPrice(REQ, TickTypeEnum.ASK, 1.0, TickAttrib())
    assert [name for name, _ in market.got] == ["on_market_error"]


def test_real_time_announced_again_lets_quotes_through() -> None:
    binding, _, market, _ = with_market_and_alarms()
    w = binding._wrapper
    w.marketDataType(REQ, 2)
    w.marketDataType(REQ, 1)  # IBKR back on live data for it
    w.tickPrice(REQ, TickTypeEnum.BID, 2.0, TickAttrib())
    quotes = [args for name, args in market.got if name == "on_quote"]
    assert quotes == [(REQ, QuoteField.BID, D(2))]


def test_a_real_time_announcement_raises_nothing() -> None:
    binding, _, market, alarms = with_market_and_alarms()
    binding._wrapper.marketDataType(REQ, 1)
    assert market.got == []
    assert alarms == []


def test_a_new_session_forgets_silenced_requests() -> None:
    binding, _, market, _ = with_market_and_alarms()
    binding._wrapper.marketDataType(REQ, 3)
    binding._wrapper.new_session()
    binding._wrapper.tickPrice(REQ, TickTypeEnum.BID, 4.0, TickAttrib())
    assert market.got[-1] == ("on_quote", (REQ, QuoteField.BID, D(4)))


# --- review round 2 ------------------------------------------------------------------------


class Raising(Events):
    """A market service with a bug: every quote raises."""

    def on_quote(self, *args: Any) -> None:
        self.got.append(("on_quote", args))
        raise RuntimeError("service bug")


def test_a_failing_market_service_voids_the_request_and_never_the_orders() -> None:
    binding, client, events, alarms = opened()
    market = Raising()
    binding.attach_market(market)
    w = binding._wrapper
    w.tickPrice(REQ, TickTypeEnum.BID, 1.0, TickAttrib())  # raises inside the service
    w.tickPrice(REQ, TickTypeEnum.ASK, 2.0, TickAttrib())  # void: dropped, no second alarm
    w.tickSize(REQ, TickTypeEnum.OPTION_PUT_OPEN_INTEREST, D(3))  # also dropped
    w.error(REQ, 0, 354, "not subscribed")  # also dropped
    assert market.got == [
        ("on_quote", (REQ, QuoteField.BID, D(1))),
        (
            "on_market_error",
            (REQ, MARKET_DATA_UNREADABLE, "handling a quote failed (RuntimeError('service bug'))"),
        ),
    ]
    assert client.connected
    assert sum("is void" in a for a in alarms) == 1
    w.orderStatus(7, "Submitted", D(0), D(1), 0.0, 77, 0, 0.0, 0, "", 0.0)
    assert events.got[-1][0] == "on_order_status"  # the order side carries on
    binding.request_quotes(REQ + 1, CALL)  # the service asks again, under a new id
    w.tickPrice(REQ + 1, TickTypeEnum.BID, 5.0, TickAttrib())
    assert market.got[-2] == ("on_quote", (REQ + 1, QuoteField.BID, D(5)))  # heard
    assert market.got[-1][0] == "on_market_error"  # (and the still-buggy service voids it)


def test_a_void_bar_request_stays_void_and_alarms_once() -> None:
    binding, _, market, alarms = with_market_and_alarms()
    w = binding._wrapper
    bad = bar(1759672800, 1.0, 1.0, 1.0, 1.0, D(-1))
    for _ in range(5):
        w.historicalDataUpdate(REQ, bad)  # every few seconds, the same fault
    w.historicalDataEnd(REQ, "", "")
    assert len(market.got) == 1
    assert sum("is void" in a for a in alarms) == 1
    w.historicalDataUpdate(REQ, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D(1)))  # even good
    assert len(market.got) == 1  # a void request stays void
    binding.request_bars(REQ + 1, NQ_FUT, "", "1 D", True)  # asked again, new id
    w.historicalDataUpdate(REQ + 1, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D(1)))
    assert market.got[-1][0] == "on_bar_update"


REQUESTS = [
    lambda b, r: b.request_contract_details(r, NQ_FUT),
    lambda b, r: b.request_option_params(r, "NQ", "CME", "FUT", 555),
    lambda b, r: b.request_quotes(r, CALL),
    lambda b, r: b.request_bars(r, NQ_FUT, "", "1 D", False),
]


@pytest.mark.parametrize("first", REQUESTS)
@pytest.mark.parametrize("again", REQUESTS)
def test_a_request_id_is_never_used_twice_in_a_session(first: Any, again: Any) -> None:
    """A cancelled request's late ticks would otherwise pass for the new one's."""
    binding, client, _, _ = with_market()
    first(binding, REQ)
    with pytest.raises(ValueError, match="already used"):
        again(binding, REQ)
    assert len(client.calls) == 1  # the reuse was never sent


def test_a_cancelled_request_id_is_still_used() -> None:
    binding, client, _, _ = with_market()
    binding.request_quotes(REQ, CALL)
    binding.cancel_quotes(REQ)
    with pytest.raises(ValueError, match="already used"):
        binding.request_quotes(REQ, CALL)
    assert [n for n, _ in client.calls] == ["reqMktData", "cancelMktData"]


@pytest.mark.parametrize("ask", REQUESTS)
def test_a_market_request_id_below_the_base_is_refused(ask: Any) -> None:
    binding, client, _, _ = with_market()
    with pytest.raises(ValueError, match="below"):
        ask(binding, MARKET_REQUEST_BASE - 1)
    ask(binding, MARKET_REQUEST_BASE)  # the base itself is a market id
    assert len(client.calls) == 1


def test_a_new_session_may_use_an_id_again() -> None:
    binding, client, _, _ = with_market()
    binding.request_quotes(REQ, CALL)
    binding._wrapper.new_session()  # IBKR forgot every request of the old session
    binding.request_quotes(REQ, CALL)
    assert [n for n, _ in client.calls] == ["reqMktData", "reqMktData"]


@pytest.mark.parametrize("epoch", ["99999999999999999999", "99999999999999"])
def test_an_impossible_epoch_is_unreadable_not_a_crash(epoch: str) -> None:
    binding, client, market, alarms = with_market_and_alarms()
    b = bar(0, 1.0, 1.0, 1.0, 1.0, D(1))
    b.date = epoch  # past what a datetime can hold (OverflowError) or a year (ValueError)
    binding._wrapper.historicalData(REQ, b)
    assert_voided(market, client, alarms, f"IBKR sent an unreadable bar (bar date '{epoch}'")


def test_the_market_hears_the_session_close_even_when_the_adapter_fails() -> None:
    class Failing(Events):
        def on_connection(self, *args: Any) -> None:
            if args[0] is Connection.LOST:
                raise RuntimeError("adapter bug")

    binding, _, _, _ = bound()
    binding.attach(Failing())
    market = Events()
    binding.attach_market(market)
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(1)
    with pytest.raises(RuntimeError, match="adapter bug"):
        binding._wrapper.closed()  # (the supervisor alarms on this and reconnects)
    assert market.got[-1] == ("on_market_connection", (Connection.LOST,))


def test_a_void_request_raises_no_data_type_alarm() -> None:
    binding, _, market, alarms = with_market_and_alarms()
    binding._wrapper.historicalData(REQ, bar(1759672800, 1.0, 1.0, 1.0, 1.0, D(-1)))
    binding._wrapper.marketDataType(REQ, 3)
    assert len(market.got) == 1
    assert not any("not real-time" in a for a in alarms)


def test_a_market_service_failing_on_connection_news_never_stops_the_session() -> None:
    class Broken(Events):
        def on_market_connection(self, *args: Any) -> None:
            raise RuntimeError("bug")

        def on_market_notice(self, *args: Any) -> None:
            raise RuntimeError("bug")

    binding, _, events, alarms = bound()
    binding.attach_market(Broken())
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(1)
    binding._wrapper.error(-1, 0, 2103, "farm broken")
    assert ("on_error", (-1, 2103, "farm broken")) in events.got
    binding._wrapper.closed()
    assert events.got[-1] == ("on_connection", (Connection.LOST,))
    assert sum("market service failed" in a for a in alarms) == 3


def test_the_supervisor_survives_any_exception_and_reconnects() -> None:
    import threading

    from footprint.brokers.ibkr.binding import IbBinding
    from tests.brokers.test_ibkr_binding import SCOPE, Recording, settings

    alarms: list[str] = []
    clients: list[Recording] = []
    stop = threading.Event()

    def factory(wrapper: Any) -> Recording:
        client = Recording(wrapper)

        def session(c: Recording) -> None:
            wrapper.managedAccounts(ACCOUNT)
            wrapper.nextValidId(100)
            if len(clients) == 1:
                raise OverflowError("anything a callback could raise")
            stop.set()

        client.on_run = session
        clients.append(client)
        return client

    binding = IbBinding(settings(reconnect_seconds=0.01), SCOPE, alarms.append, factory)
    events = Events()
    binding.attach(events)
    binding.run(stop)
    assert len(clients) == 2  # the failed session was followed by a new one
    assert not clients[0].connected  # and it was hung up
    assert any("session failed unexpectedly" in a for a in alarms)
    connections = [args[0] for name, args in events.got if name == "on_connection"]
    assert connections == [Connection.CONNECTED, Connection.LOST] * 2
