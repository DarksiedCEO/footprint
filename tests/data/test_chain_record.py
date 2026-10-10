"""Q3 slice 1: the option-chain recorder's file format (ibapi-free). One CSV per UTC day
under the output directory; a snapshot is written whole or not at all; a quote older than
the configured age, or newer than the snapshot, is never written; unknown open interest
is a blank cell, never 0. Hand-built rows; nothing here says anything about prices."""

from __future__ import annotations

import csv
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.options import Right
from footprint.data.chain_record import HEADER, ChainRow, day_file, write_snapshot

D = Decimal
TS = datetime(2026, 10, 14, 15, 30, 0, tzinfo=UTC)
MAX_AGE = 5


def row(
    strike: str = "20000",
    right: Right = Right.CALL,
    age: timedelta = timedelta(seconds=1, milliseconds=250),
    oi: int | None = 120,
    ts: datetime = TS,
) -> ChainRow:
    return ChainRow(
        ts=ts,
        future_bid=D("20010.25"),
        future_ask=D("20010.75"),
        contract_id=f"NQ-{strike}-{right.value}",
        expiry=date(2026, 10, 16),
        strike=D(strike),
        right=right,
        bid=D("101.5"),
        ask=D("103.25"),
        quote_ts=ts - age,
        open_interest=oi,
    )


def read(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_a_snapshot_is_written_with_a_header_one_line_per_quote(tmp_path: Path) -> None:
    path = write_snapshot(tmp_path, [row(), row("20025", Right.PUT, oi=None)], MAX_AGE)
    assert path == tmp_path / "chain-2026-10-14.csv"
    assert read(path) == [
        list(HEADER),
        [
            "2026-10-14T15:30:00+00:00",
            "20010.25",
            "20010.75",
            "NQ-20000-CALL",
            "2026-10-16",
            "20000",
            "CALL",
            "101.5",
            "103.25",
            "1.250",
            "120",
        ],
        [
            "2026-10-14T15:30:00+00:00",
            "20010.25",
            "20010.75",
            "NQ-20025-PUT",
            "2026-10-16",
            "20025",
            "PUT",
            "101.5",
            "103.25",
            "1.250",
            "",
        ],
    ]


def test_zero_open_interest_is_written_as_zero_not_blank(tmp_path: Path) -> None:
    path = write_snapshot(tmp_path, [row(oi=0)], MAX_AGE)
    assert read(path)[1][-1] == "0"


def test_a_later_snapshot_is_appended_without_a_second_header(tmp_path: Path) -> None:
    write_snapshot(tmp_path, [row()], MAX_AGE)
    later = TS + timedelta(minutes=1)
    path = write_snapshot(tmp_path, [row(ts=later)], MAX_AGE)
    lines = read(path)
    assert [line[0] for line in lines] == ["ts", TS.isoformat(), later.isoformat()]


def test_each_utc_day_has_its_own_file(tmp_path: Path) -> None:
    write_snapshot(tmp_path, [row(ts=datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC))], MAX_AGE)
    write_snapshot(tmp_path, [row(ts=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC))], MAX_AGE)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "chain-2026-10-14.csv",
        "chain-2026-10-15.csv",
    ]
    assert day_file(tmp_path, TS) == tmp_path / "chain-2026-10-14.csv"


def test_a_quote_exactly_the_configured_age_is_written(tmp_path: Path) -> None:
    path = write_snapshot(tmp_path, [row(age=timedelta(seconds=MAX_AGE))], MAX_AGE)
    assert read(path)[1][9] == "5.000"


def test_a_stale_quote_refuses_the_whole_snapshot(tmp_path: Path) -> None:
    stale = row("20025", age=timedelta(seconds=MAX_AGE, microseconds=1))
    with pytest.raises(ValueError, match="older than"):
        write_snapshot(tmp_path, [row(), stale], MAX_AGE)
    assert list(tmp_path.iterdir()) == []


def test_a_quote_newer_than_the_snapshot_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="after the snapshot"):
        write_snapshot(tmp_path, [row(age=timedelta(microseconds=-1))], MAX_AGE)
    assert list(tmp_path.iterdir()) == []


def test_a_quote_stamped_at_the_snapshot_itself_is_written(tmp_path: Path) -> None:
    path = write_snapshot(tmp_path, [row(age=timedelta(0))], MAX_AGE)
    assert read(path)[1][9] == "0.000"


def test_an_empty_snapshot_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no quotes"):
        write_snapshot(tmp_path, [], MAX_AGE)
    assert list(tmp_path.iterdir()) == []


def test_rows_from_two_moments_are_not_one_snapshot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one moment"):
        write_snapshot(tmp_path, [row(), row(ts=TS + timedelta(seconds=1))], MAX_AGE)
    assert list(tmp_path.iterdir()) == []


def test_a_file_with_another_header_is_refused_and_kept(tmp_path: Path) -> None:
    path = day_file(tmp_path, TS)
    path.write_text("something,else\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        write_snapshot(tmp_path, [row()], MAX_AGE)
    assert path.read_text(encoding="utf-8") == "something,else\n1,2\n"


def test_an_empty_existing_file_gets_its_header(tmp_path: Path) -> None:
    path = day_file(tmp_path, TS)
    path.write_text("", encoding="utf-8")
    write_snapshot(tmp_path, [row()], MAX_AGE)
    assert read(path)[0] == list(HEADER)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"ts": datetime(2026, 10, 14, 15, 30)}, "ts"),  # noqa: DTZ001 - naive on purpose
        ({"bid": D("104")}, "crossed"),
        ({"bid": D("-1")}, "bid"),
        ({"future_bid": D("0")}, "future_bid"),
        ({"future_ask": D("20010")}, "crossed"),
        ({"strike": D("0")}, "strike"),
        ({"open_interest": -1}, "open_interest"),
        ({"open_interest": True}, "open_interest"),
        ({"ask": D("NaN")}, "ask"),
    ],
)
def test_a_row_that_is_not_a_quote_is_refused(change: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        replace(row(), **change)  # type: ignore[arg-type]


def test_every_snapshot_is_on_disk_when_the_call_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = os.fsync

    def fsync(fd: int) -> None:
        seen.append(day_file(tmp_path, TS).read_text(encoding="utf-8"))
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    write_snapshot(tmp_path, [row(), row("20025")], MAX_AGE)
    assert len(seen) == 1
    assert seen[0].count("NQ-20") == 2  # flushed before the fsync, every row there
