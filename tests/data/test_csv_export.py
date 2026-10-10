"""Q2 (first slice): minute bars written in exactly the format CsvBarSource reads, never
over an existing file, and never left looking complete when the export failed."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.data.csv_export import ExportRefused, partial_path, write_bars
from footprint.data.file_source import COLUMNS, CsvBarSource

D = Decimal
T0 = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)
MIN = timedelta(minutes=1)


def bar(i: int, symbol: str = "NQ", volume: int = 7) -> Bar:
    p = D("20025.25") + D(i) / 4
    return Bar(symbol, T0 + i * MIN, p, p + D("1.5"), p - D("0.75"), p + D("0.25"), volume)


def test_an_export_reads_back_as_the_identical_bars(tmp_path: Path) -> None:
    bars = [bar(0, volume=0), bar(1), bar(2, volume=123456), bar(60 * 26)]  # a gap is kept
    out = tmp_path / "nq_1m.csv"
    assert write_bars(bars, out) == len(bars)
    assert list(CsvBarSource(out, "NQ").bars()) == bars
    assert not partial_path(out).exists()


def test_the_file_has_exactly_the_reader_columns_in_order(tmp_path: Path) -> None:
    out = tmp_path / "b.csv"
    write_bars([bar(0)], out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(COLUMNS)
    assert lines[1] == "2026-10-06T13:30:00+00:00,20025.25,20026.75,20024.50,20025.50,7"
    assert len(lines) == 2


def test_an_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "b.csv"
    out.write_text("keep me", encoding="utf-8")
    with pytest.raises(ExportRefused, match="already exists"):
        write_bars([bar(0)], out)
    assert out.read_text(encoding="utf-8") == "keep me"
    assert not partial_path(out).exists()


def test_a_left_over_partial_file_blocks_the_export(tmp_path: Path) -> None:
    out = tmp_path / "b.csv"
    partial_path(out).write_text("from a failed run", encoding="utf-8")
    with pytest.raises(ExportRefused, match=r"\.partial"):
        write_bars([bar(0)], out)
    assert partial_path(out).read_text(encoding="utf-8") == "from a failed run"
    assert not out.exists()


def test_a_failure_mid_export_leaves_only_a_partial_file(tmp_path: Path) -> None:
    out = tmp_path / "b.csv"

    def broken() -> Iterator[Bar]:
        yield bar(0)
        yield bar(1)
        raise RuntimeError("IBKR 162: pacing violation")

    with pytest.raises(RuntimeError, match="pacing"):
        write_bars(broken(), out)
    assert not out.exists()
    assert partial_path(out).name == "b.csv.partial"
    assert partial_path(out).exists()


@pytest.mark.parametrize("second", [0, -1], ids=["same minute", "earlier"])
def test_bars_out_of_order_fail_closed(tmp_path: Path, second: int) -> None:
    out = tmp_path / "b.csv"
    with pytest.raises(ValueError, match="not later"):
        write_bars([bar(0), bar(1), bar(1 + second)], out)
    assert not out.exists()
    assert partial_path(out).exists()


def test_bars_of_two_symbols_fail_closed(tmp_path: Path) -> None:
    out = tmp_path / "b.csv"
    with pytest.raises(ValueError, match="symbol"):
        write_bars([bar(0), bar(1, symbol="ES")], out)
    assert not out.exists()


def test_no_bars_at_all_is_a_failure_not_an_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "b.csv"
    with pytest.raises(ValueError, match="no bars"):
        write_bars([], out)
    assert not out.exists()


def test_a_file_that_appears_during_the_export_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "b.csv"
    real_fsync = os.fsync

    def fsync_then_someone_writes(fd: int) -> None:
        real_fsync(fd)
        out.write_text("written meanwhile", encoding="utf-8")

    monkeypatch.setattr(os, "fsync", fsync_then_someone_writes)
    with pytest.raises(FileExistsError):
        write_bars([bar(0)], out)
    assert out.read_text(encoding="utf-8") == "written meanwhile"
    assert partial_path(out).exists()


def test_every_bar_is_on_disk_when_the_file_is_synced_before_it_gets_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G27-3: fsync only makes durable what already left Python's buffer. If the bars
    # were still buffered at the fsync, a power loss after the link could leave a
    # truncated file under the finished name.
    out = tmp_path / "b.csv"
    bars = [bar(i) for i in range(3)]
    seen: list[str] = []
    real_fsync = os.fsync

    def fsync_reading_the_partial(fd: int) -> None:
        seen.append(partial_path(out).read_text(encoding="utf-8"))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_reading_the_partial)
    assert write_bars(bars, out) == 3
    assert seen == [out.read_text(encoding="utf-8")]  # all of it, at the sync
    assert len(seen[0].splitlines()) == 1 + len(bars)
