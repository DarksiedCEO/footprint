"""Q3 slice 2: a recorded chain becomes exactly the rows the day file holds (ibapi-free).
Hand-built quotes; nothing here says anything about prices."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.brokers.ibkr.chain_recorder import snapshot_rows
from footprint.brokers.ibkr.market import RecordedChain, RecordedOption
from footprint.core.options import Right
from footprint.data.chain_record import write_snapshot
from footprint.execution.orders import Quote

D = Decimal
NOW = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)


def recorded() -> RecordedChain:
    return RecordedChain(
        future=Quote("NQ-FUT", D("20025.00"), D("20025.50"), NOW - timedelta(seconds=1)),
        options=(
            RecordedOption(
                Quote("NQ-C-20020", D(150), D(151), NOW - timedelta(seconds=2)),
                date(2026, 10, 16),
                D(20020),
                Right.CALL,
                1234,
            ),
            RecordedOption(
                Quote("NQ-P-20030", D(0), D(141), NOW - timedelta(seconds=3)),
                date(2026, 10, 16),
                D(20030),
                Right.PUT,
                None,
            ),
        ),
    )


def test_each_option_becomes_one_row_with_the_future_and_its_own_times() -> None:
    rows = snapshot_rows(NOW, recorded())
    assert [
        (
            r.ts,
            r.future_bid,
            r.future_ask,
            r.future_quote_ts,
            r.contract_id,
            r.expiry,
            r.strike,
            r.right,
            r.bid,
            r.ask,
            r.quote_ts,
            r.open_interest,
        )
        for r in rows
    ] == [
        (
            NOW,
            D("20025.00"),
            D("20025.50"),
            NOW - timedelta(seconds=1),
            "NQ-C-20020",
            date(2026, 10, 16),
            D(20020),
            Right.CALL,
            D(150),
            D(151),
            NOW - timedelta(seconds=2),
            1234,
        ),
        (
            NOW,
            D("20025.00"),
            D("20025.50"),
            NOW - timedelta(seconds=1),
            "NQ-P-20030",
            date(2026, 10, 16),
            D(20030),
            Right.PUT,
            D(0),
            D(141),
            NOW - timedelta(seconds=3),
            None,
        ),
    ]


def test_no_chain_gives_no_rows() -> None:
    assert snapshot_rows(NOW, None) == []


def test_a_naive_snapshot_time_is_refused() -> None:
    with pytest.raises(ValueError, match="now"):
        snapshot_rows(datetime(2026, 10, 14, 15, 30), recorded())  # noqa: DTZ001


def test_the_rows_are_written_as_one_snapshot(tmp_path: Path) -> None:
    path = write_snapshot(tmp_path, snapshot_rows(NOW, recorded()), 5)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert lines[1].split(",")[3] == "1.000"  # the future's own age
    assert lines[2].split(",")[-1] == ""  # open interest IBKR did not send
