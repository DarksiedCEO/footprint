from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.interfaces import DataSource
from footprint.data.file_source import CsvBarSource

HEADER = "ts,open,high,low,close,volume\n"


def write(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "bars.csv"
    path.write_text(header + body)
    return path


def test_reads_bars_and_converts_to_utc(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "2026-01-05T14:30:00+00:00,10,12,9,11.25,5\n2026-01-05T06:31:00-08:00,11.25,11.5,11,11,7\n",
    )
    bars = list(CsvBarSource(path, symbol="TEST").bars())
    assert [bar.ts for bar in bars] == [
        datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 5, 14, 31, tzinfo=UTC),
    ]
    assert bars[0].symbol == "TEST"
    assert bars[0].close == Decimal("11.25")
    assert bars[1].volume == 7


def test_satisfies_the_data_source_seam(tmp_path: Path) -> None:
    assert isinstance(CsvBarSource(write(tmp_path, ""), symbol="TEST"), DataSource)


@pytest.mark.parametrize(
    "row",
    [
        "2026-01-05T14:30:00,10,12,9,11,5",  # naive timestamp
        "not-a-date,10,12,9,11,5",
        "2026-01-05T14:30:00+00:00,10,8,9,11,5",  # high below low
        "2026-01-05T14:30:00+00:00,ten,12,9,11,5",
        "2026-01-05T14:30:00+00:00,10,12,9,11,5.5",
        "2026-01-05T14:30:00+00:00,10,12,9",  # short row
    ],
)
def test_bad_row_fails_loudly_with_line_number(tmp_path: Path, row: str) -> None:
    path = write(tmp_path, row + "\n")
    with pytest.raises(ValueError, match=r"bars\.csv:2: "):
        list(CsvBarSource(path, symbol="TEST").bars())


def test_missing_column_fails(tmp_path: Path) -> None:
    path = write(tmp_path, "", header="ts,open,high,low,close\n")
    with pytest.raises(ValueError, match="missing columns"):
        list(CsvBarSource(path, symbol="TEST").bars())
