"""Review R12: a decision and a simulated fill always see the same market."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.execution.market import QuoteBook
from footprint.execution.orders import Order, OrderType, Quote, Side, SimulatedBroker

D = Decimal
T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def test_the_book_drops_quotes_that_go_back_in_time_without_raising() -> None:
    book = QuoteBook()
    assert book.publish(Quote("C", D(1), D(2), T))
    assert not book.publish(Quote("C", D(5), D(6), T - timedelta(seconds=1)))
    assert book.latest("C") == Quote("C", D(1), D(2), T)


def test_a_failing_listener_does_not_stop_the_others() -> None:
    book = QuoteBook()
    heard: list[Quote] = []

    def broken(quote: Quote) -> None:
        raise RuntimeError("boom")

    book.subscribe(broken)
    book.subscribe(heard.append)
    book.publish(Quote("C", D(1), D(2), T))
    assert len(heard) == 1


def test_the_simulator_never_fills_on_a_stale_quote() -> None:
    book = QuoteBook()
    broker = SimulatedBroker(book, 5)
    book.publish(Quote("C", D(6), D(7), T))
    book.publish(Quote("OTHER", D(1), D(2), T + timedelta(hours=6)))  # the market moved on
    status = broker.place(Order("x", "C", Side.SELL, 1, OrderType.LIMIT, D(6)))
    assert status.filled == 0


def test_an_empty_book_reads_as_stale_and_bidless() -> None:
    q = QuoteBook().latest_or_empty("C")
    assert (q.bid, q.ask) == (D(0), D(0))
    assert q.ts < T


def test_the_simulator_fills_only_at_the_published_market() -> None:
    book = QuoteBook()
    broker = SimulatedBroker(book, 5)
    book.publish(Quote("C", D(114), D(116), T))
    status = broker.place(Order("x", "C", Side.SELL, 1, OrderType.LIMIT, D(114)))
    assert status.avg_price == D(114)  # the bid everyone saw, nothing better
    book.publish(Quote("C", D(150), D(151), T + timedelta(seconds=1)))
    buy = broker.place(Order("y", "C", Side.BUY, 1, OrderType.LIMIT, D(152)))
    assert buy.avg_price == D(151)


def test_the_simulator_cannot_be_built_without_the_shared_book() -> None:
    with pytest.raises(TypeError):
        SimulatedBroker()  # type: ignore[call-arg]


def test_a_dead_single_contract_feed_fills_nothing_once_the_clock_moves_on() -> None:
    """Review N4: with one contract and no other quotes, the clock alone marks it stale."""
    book = QuoteBook()
    broker = SimulatedBroker(book, 5)
    book.publish(Quote("C", D(6), D(7), T))
    book.mark_time(T + timedelta(seconds=6))  # the feed went quiet
    status = broker.place(Order("x", "C", Side.SELL, 1, OrderType.LIMIT, D(6)))
    assert status.filled == 0
    book.mark_time(T)  # the clock never runs backwards
    assert book.newest == T + timedelta(seconds=6)


def test_a_fresh_single_contract_feed_still_fills() -> None:
    book = QuoteBook()
    broker = SimulatedBroker(book, 5)
    book.publish(Quote("C", D(6), D(7), T))
    book.mark_time(T + timedelta(seconds=5))  # exactly at the limit: still fresh
    status = broker.place(Order("x", "C", Side.SELL, 1, OrderType.LIMIT, D(6)))
    assert status.filled == 1
