from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from footprint.research.bars import group_days, load_days, session_day
from footprint.research.sessions import classify, names
from footprint.research.settings import OTHER, ResearchSettings, Window
from footprint.research.vol import load_vol
from tests.research.conftest import NY, write_bars

ROLL = time(18, 0)


# --- session day -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2024-03-04T17:59:00-05:00", date(2024, 3, 4)),  # last minute before the roll
        ("2024-03-04T18:00:00-05:00", date(2024, 3, 5)),  # the roll starts the next day
        ("2024-03-04T23:59:00-05:00", date(2024, 3, 5)),
        ("2024-03-05T00:00:00-05:00", date(2024, 3, 5)),
        ("2024-03-05T09:30:00-05:00", date(2024, 3, 5)),
        ("2024-03-04T23:00:00+00:00", date(2024, 3, 5)),  # 18:00 New York, written in UTC
        ("2024-03-04T22:59:00+00:00", date(2024, 3, 4)),
        # Summer time: 18:00 New York is 22:00 UTC.
        ("2024-07-01T22:00:00+00:00", date(2024, 7, 2)),
        ("2024-07-01T21:59:00+00:00", date(2024, 7, 1)),
        # Friday evening belongs to Saturday's date (no bars there in practice).
        ("2024-03-08T18:30:00-05:00", date(2024, 3, 9)),
    ],
)
def test_session_day_rolls_at_the_configured_time(stamp: str, expected: date) -> None:
    assert session_day(datetime.fromisoformat(stamp), NY, ROLL) == expected


def test_session_day_follows_the_configured_roll() -> None:
    ts = datetime.fromisoformat("2024-03-04T17:00:00-05:00")
    assert session_day(ts, NY, ROLL) == date(2024, 3, 4)
    assert session_day(ts, NY, time(17, 0)) == date(2024, 3, 5)
    assert session_day(ts, NY, time(16, 59)) == date(2024, 3, 5)


def test_session_day_reads_the_roll_minutes() -> None:
    roll = time(17, 30)
    assert session_day(datetime.fromisoformat("2024-03-04T17:29:00-05:00"), NY, roll) == date(
        2024, 3, 4
    )
    assert session_day(datetime.fromisoformat("2024-03-04T17:30:00-05:00"), NY, roll) == date(
        2024, 3, 5
    )


def test_session_day_refuses_a_naive_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        session_day(datetime.fromisoformat("2024-03-04T12:00:00"), NY, ROLL)


def test_session_day_uses_the_given_zone() -> None:
    ts = datetime.fromisoformat("2024-03-04T22:30:00+00:00")  # 17:30 New York, 23:30 London
    assert session_day(ts, NY, ROLL) == date(2024, 3, 4)
    assert session_day(ts, ZoneInfo("Europe/London"), ROLL) == date(2024, 3, 5)


# --- loading bars ------------------------------------------------------------------


def test_load_days_groups_by_session_day_as_floats(tmp_path: Path) -> None:
    path = write_bars(
        tmp_path,
        [
            "2024-03-04T17:58:00-05:00,100,101,99,100.5,7",
            "2024-03-04T17:59:00-05:00,100.5,102,100,101.25,8",
            "2024-03-04T18:00:00-05:00,101.25,103,101,102,9",
            "2024-03-05T03:00:00-05:00,102,104,101.5,103.75,10",
        ],
    )
    days = load_days(path, "NQ", NY, ROLL)
    assert [d.day for d in days] == [date(2024, 3, 4), date(2024, 3, 5)]
    first, second = days
    assert len(first) == 2
    assert len(second) == 2
    assert first.close == (100.5, 101.25)
    assert first.high == (101.0, 102.0)
    assert first.low == (99.0, 100.0)
    assert first.open == (100.0, 100.5)
    assert first.volume == (7, 8)
    assert all(isinstance(x, float) for x in first.close)
    assert second.clock == (time(18, 0), time(3, 0))
    assert second.ts[0] == datetime.fromisoformat("2024-03-04T23:00:00+00:00")
    assert second.ts[0].tzinfo is UTC


@pytest.mark.parametrize(
    "rows",
    [
        ["2024-03-04T10:00:00-05:00,100,99,101,100,1"],  # high below low
        ["2024-03-04T10:00:00,100,101,99,100,1"],  # no offset
        ["2024-03-04T10:00:00-05:00,100,101,99,,1"],  # missing close
        ["2024-03-04T10:00:00-05:00,0,101,0,100,1"],  # price not > 0
        [
            "2024-03-04T10:01:00-05:00,100,101,99,100,1",
            "2024-03-04T10:00:00-05:00,100,101,99,100,1",
        ],  # going back in time
        [
            "2024-03-04T10:00:00-05:00,100,101,99,100,1",
            "2024-03-04T10:00:00-05:00,100,101,99,100,1",
        ],  # duplicate
    ],
)
def test_bad_rows_fail_loudly_with_the_line(tmp_path: Path, rows: list[str]) -> None:
    path = write_bars(tmp_path, rows)
    with pytest.raises(ValueError, match=r"bars\.csv:\d"):
        load_days(path, "NQ", NY, ROLL)


def test_group_days_refuses_out_of_order_bars(tmp_path: Path) -> None:
    from footprint.data.file_source import CsvBarSource

    path = write_bars(
        tmp_path,
        [
            "2024-03-04T10:00:00-05:00,100,101,99,100,1",
            "2024-03-04T10:01:00-05:00,100,101,99,100,1",
        ],
    )
    bars = list(CsvBarSource(path, "NQ").bars())
    with pytest.raises(ValueError, match="out of order"):
        group_days(reversed(bars), NY, ROLL)
    with pytest.raises(ValueError, match="out of order"):
        group_days([bars[0], bars[0]], NY, ROLL)


def test_group_days_of_nothing_is_empty() -> None:
    assert group_days([], NY, ROLL) == []


# --- sessions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("clock", "label"),
    [
        (time(18, 0), "asia"),
        (time(23, 59), "asia"),
        (time(0, 0), "asia"),
        (time(2, 59), "asia"),
        (time(3, 0), "london"),
        (time(8, 24), "london"),
        (time(8, 25), OTHER),
        (time(9, 29), OTHER),
        (time(9, 30), "new_york"),
        (time(15, 59), "new_york"),
        (time(16, 0), OTHER),
        (time(17, 59), OTHER),
    ],
)
def test_configured_sessions_label_every_edge(
    research: ResearchSettings, clock: time, label: str
) -> None:
    assert classify(clock, research.sessions) == label


def test_session_names_in_config_order_other_last(research: ResearchSettings) -> None:
    assert names(research.sessions) == ("asia", "london", "new_york", OTHER)


def test_window_without_wrap_is_half_open() -> None:
    w = Window(time(3, 0), time(8, 25))
    assert w.covers(time(3, 0))
    assert w.covers(time(8, 24))
    assert not w.covers(time(8, 25))
    assert not w.covers(time(2, 59))


def test_window_with_wrap_is_half_open() -> None:
    w = Window(time(18, 0), time(3, 0))
    assert w.covers(time(18, 0))
    assert w.covers(time(2, 59))
    assert w.covers(time(0, 0))
    assert not w.covers(time(3, 0))
    assert not w.covers(time(17, 59))
    assert not w.covers(time(12, 0))


def test_classify_with_no_sessions_is_other() -> None:
    assert classify(time(12, 0), ()) == OTHER


# --- volatility index --------------------------------------------------------------

VOL_HEADER = "DATE,OPEN,HIGH,LOW,CLOSE\n"


def _vol(tmp_path: Path, body: str, header: str = VOL_HEADER) -> Path:
    path = tmp_path / "VXN_History.csv"
    path.write_text(header + body)
    return path


def test_lookup_uses_the_close_strictly_before_the_day(tmp_path: Path) -> None:
    path = _vol(tmp_path, "03/01/2024,19,21,18,20.5\n03/04/2024,20,23,19,22\n03/05/2024,1,1,1,40\n")
    vol = load_vol(path, divisor=100, max_age_days=5)
    assert vol.lookup(date(2024, 3, 5)) == pytest.approx(0.22)  # never the same day's 40
    assert vol.lookup(date(2024, 3, 4)) == pytest.approx(0.205)  # Friday's, over the weekend
    assert vol.lookup(date(2024, 3, 6)) == pytest.approx(0.40)
    assert vol.lookup(date(2024, 3, 1)) is None  # nothing before the first date
    assert vol.lookup(date(2024, 2, 1)) is None


def test_lookup_skips_stale_values_at_the_configured_age(tmp_path: Path) -> None:
    vol = load_vol(_vol(tmp_path, "03/01/2024,1,1,1,20\n"), divisor=100, max_age_days=5)
    assert vol.lookup(date(2024, 3, 6)) == pytest.approx(0.20)  # five days: still usable
    assert vol.lookup(date(2024, 3, 7)) is None  # six days: stale, never guessed
    tight = load_vol(_vol(tmp_path, "03/01/2024,1,1,1,20\n"), divisor=100, max_age_days=1)
    assert tight.lookup(date(2024, 3, 2)) == pytest.approx(0.20)
    assert tight.lookup(date(2024, 3, 3)) is None


def test_divisor_is_applied(tmp_path: Path) -> None:
    vol = load_vol(_vol(tmp_path, "03/01/2024,1,1,1,20\n"), divisor=10, max_age_days=5)
    assert vol.lookup(date(2024, 3, 2)) == pytest.approx(2.0)


def test_header_is_case_and_space_tolerant_and_blank_lines_skipped(tmp_path: Path) -> None:
    path = _vol(tmp_path, "03/01/2024,1,1,1,20\n\n", header="Date, Open,High,Low,Close\n")
    assert load_vol(path, 100, 5).closes == (20.0,)


@pytest.mark.parametrize(
    "body",
    [
        "2024-03-01,1,1,1,20\n",  # wrong date format
        "03/01/2024,1,1,1,abc\n",  # not a number
        "03/01/2024,1,1,1,0\n",  # not > 0
        "03/01/2024,1,1,1,-3\n",
        "03/01/2024,1,1,1,nan\n",
        "03/01/2024,1,1,1,inf\n",
        "03/01/2024,1,1,20\n",  # too few values
        "03/02/2024,1,1,1,20\n03/01/2024,1,1,1,20\n",  # backwards
        "03/01/2024,1,1,1,20\n03/01/2024,1,1,1,21\n",  # duplicate
    ],
)
def test_bad_vol_rows_fail_loudly(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match=r"VXN_History\.csv:\d"):
        load_vol(_vol(tmp_path, body), 100, 5)


def test_bad_vol_header_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="header"):
        load_vol(_vol(tmp_path, "03/01/2024,20\n", header="DATE,CLOSE\n"), 100, 5)
    with pytest.raises(ValueError, match="header"):
        load_vol(_vol(tmp_path, "", header=""), 100, 5)
