"""Q2: the `history` export driver. It steps MarketData (read-only) until the whole
history has loaded, refuses a history with a gap, and only then writes the CSV the
backtest reads. A timeout, a gap or a write failure exits 1 and leaves no file under the
finished name. Nothing here needs ibapi."""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.brokers.ibkr.history_export import (
    HistorySource,
    export_history,
    window_start,
    with_days,
)
from footprint.core.config import (
    ConfigError,
    InstrumentProfile,
    Profiles,
    load_execution,
    load_instrument,
    load_profiles,
)
from footprint.core.models import Bar
from footprint.data.csv_export import partial_path
from footprint.data.file_source import CsvBarSource
from tests.conftest import REPO_ROOT

MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)
NOW = datetime(2026, 10, 6, 16, 0, 7, tzinfo=UTC)  # a Tuesday, 09:00 PT
START = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
LAST = datetime(2026, 10, 6, 15, 59, tzinfo=UTC)  # the last minute closed at NOW
D = Decimal


@pytest.fixture(scope="module")
def nq() -> InstrumentProfile:
    return load_instrument(REPO_ROOT / "config" / "instruments" / "nq.yaml")


def bars_between(
    first: datetime, last: datetime, skip: frozenset[datetime] = frozenset()
) -> list[Bar]:
    out = []
    m = first
    i = 0
    while m <= last:
        if m not in skip:
            p = D("20000") + D(i) / 4
            out.append(Bar("NQ", m, p, p + 1, p - 1, p, 10 + i))
        m += MIN
        i += 1
    return out


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class Source:
    """MarketData's two calls the driver uses: bars appear from step `ready_at` on."""

    def __init__(self, bars: Sequence[Bar], ready_at: int = 0) -> None:
        self._bars = tuple(bars)
        self.ready_at = ready_at
        self.steps: list[datetime] = []

    def step(self, now: datetime) -> None:
        self.steps.append(now)

    def bars(self, now: datetime) -> Sequence[Bar]:
        return self._bars if len(self.steps) > self.ready_at else ()


def run(
    nq: InstrumentProfile,
    source: HistorySource,
    out: Path,
    clock: Clock,
    *,
    wait: float = 60,
    max_missing: int = 0,
    poll: float = 1,
    window_from: Callable[[datetime], datetime] = lambda now: START,
) -> tuple[int, str]:
    text = io.StringIO()
    code = export_history(
        source,
        out,
        window_from,
        nq,
        wait,
        max_missing,
        poll,
        lambda line: print(line, file=text),
        clock,
        clock.sleep,
    )
    return code, text.getvalue()


def test_a_complete_history_is_written_and_reads_back_identically(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    bars = bars_between(START, START + 59 * MIN)
    out = tmp_path / "nq_1m.csv"
    clock = Clock()
    code, text = run(nq, Source(bars, ready_at=3), out, clock)
    assert code == 0
    assert list(CsvBarSource(out, "NQ").bars()) == bars
    assert clock.slept == [1, 1, 1]
    assert text.splitlines()[-1] == (
        f"wrote 60 bars, {bars[0].ts.isoformat()} to {bars[-1].ts.isoformat()}, to {out}"
    )


def test_a_history_that_never_loads_exits_one_and_writes_nothing(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    out = tmp_path / "nq_1m.csv"
    clock = Clock()
    code, text = run(nq, Source(bars_between(START, START), ready_at=10_000), out, clock, wait=2.5)
    assert code == 1
    assert clock.slept == [1, 1, 0.5]
    assert clock.now == NOW + 2.5 * SEC
    assert text.splitlines()[-1] == (
        "NO HISTORY after 2.5 s: MarketData has not finished loading it "
        "(IBKR's reason, if it sent one, is in the ALARM lines above); nothing written"
    )
    assert not out.exists()
    assert not partial_path(out).exists()


def test_the_history_is_looked_at_once_more_at_the_deadline(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    out = tmp_path / "nq_1m.csv"
    clock = Clock()
    code, _ = run(nq, Source(bars_between(START, LAST), ready_at=2), out, clock, wait=2)
    assert code == 0
    assert clock.slept == [1, 1]


def test_a_wait_of_zero_still_looks_once(nq: InstrumentProfile, tmp_path: Path) -> None:
    out = tmp_path / "nq_1m.csv"
    clock = Clock()
    src = Source(bars_between(START, LAST))
    code, _ = run(nq, src, out, clock, wait=0)
    assert code == 0
    assert len(src.steps) == 1
    assert clock.slept == []


def test_a_history_with_a_gap_is_refused_and_nothing_is_written(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    hole = frozenset(START + (10 + i) * MIN for i in range(3))
    out = tmp_path / "nq_1m.csv"
    code, text = run(nq, Source(bars_between(START, LAST, hole)), out, Clock(), max_missing=2)
    assert code == 1
    last = text.splitlines()[-1]
    assert last.startswith("REFUSED: the history has a gap: 3 tradable minute(s) from ")
    assert last.endswith("; nothing written")
    assert not out.exists()
    assert not partial_path(out).exists()


def test_the_gap_allowance_is_the_configured_number(nq: InstrumentProfile, tmp_path: Path) -> None:
    hole = frozenset(START + (10 + i) * MIN for i in range(3))
    out = tmp_path / "nq_1m.csv"
    code, _ = run(nq, Source(bars_between(START, LAST, hole)), out, Clock(), max_missing=3)
    assert code == 0
    assert len(list(CsvBarSource(out, "NQ").bars())) == 57


def test_a_missing_start_is_a_gap(nq: InstrumentProfile, tmp_path: Path) -> None:
    out = tmp_path / "nq_1m.csv"
    code, text = run(nq, Source(bars_between(START + 5 * MIN, LAST)), out, Clock())
    assert code == 1
    assert "5 tradable minute(s) from 2026-10-06T15:00:00+00:00" in text
    assert not out.exists()


def test_a_missing_tail_is_a_gap(nq: InstrumentProfile, tmp_path: Path) -> None:
    # G29-2: e.g. IBKR answered the first poll "no data": today's minutes never arrived
    out = tmp_path / "nq_1m.csv"
    code, text = run(nq, Source(bars_between(START, LAST - 3 * MIN)), out, Clock(), max_missing=2)
    assert code == 1
    assert "3 tradable minute(s) from 2026-10-06T15:57:00+00:00" in text
    assert not out.exists()
    assert not partial_path(out).exists()


def test_the_tail_is_judged_by_the_same_allowance(nq: InstrumentProfile, tmp_path: Path) -> None:
    out = tmp_path / "nq_1m.csv"
    code, _ = run(nq, Source(bars_between(START, LAST - 3 * MIN)), out, Clock(), max_missing=3)
    assert code == 0


class MovingWindow:
    """MarketData's series starts at 00:00 UTC `days` before the day it is read on
    (market.py `_assemble`), and is empty until `ready`."""

    def __init__(self, days: int, ready: datetime) -> None:
        self.days, self.ready = days, ready

    def step(self, now: datetime) -> None:
        pass

    def bars(self, now: datetime) -> Sequence[Bar]:
        if now < self.ready:
            return ()
        last = now.replace(second=0, microsecond=0) - MIN
        return bars_between(window_start(now, self.days), last)


@pytest.mark.parametrize("days", [1, 2, 4])
def test_an_export_that_crosses_midnight_utc_reads_the_window_it_was_given(
    nq: InstrumentProfile, tmp_path: Path, days: int
) -> None:
    # G29-1: begun Tue 23:50 UTC (16:50 PT), loaded 00:10 UTC: the window moved a day
    clock = Clock()
    clock.now = datetime(2026, 10, 6, 23, 50, tzinfo=UTC)
    source = MovingWindow(days, datetime(2026, 10, 7, 0, 10, tzinfo=UTC))
    out = tmp_path / "nq_1m.csv"
    code, text = run(
        nq, source, out, clock, wait=1800, poll=60, window_from=lambda now: window_start(now, days)
    )
    assert code == 0, text
    written = list(CsvBarSource(out, "NQ").bars())
    assert written[0].ts == window_start(clock.now, days)
    assert written[-1].ts == datetime(2026, 10, 7, 0, 9, tzinfo=UTC)


def test_an_existing_file_is_refused_before_anything_is_loaded(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    out = tmp_path / "nq_1m.csv"
    out.write_text("keep me", encoding="utf-8")
    src = Source(bars_between(START, START + MIN))
    code, text = run(nq, src, out, Clock())
    assert code == 1
    assert src.steps == []  # refused before connecting time is spent
    assert "already exists" in text
    assert out.read_text(encoding="utf-8") == "keep me"


def test_a_left_over_partial_is_refused_before_anything_is_loaded(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    out = tmp_path / "nq_1m.csv"
    partial_path(out).write_text("old", encoding="utf-8")
    src = Source(bars_between(START, START + MIN))
    code, text = run(nq, src, out, Clock())
    assert code == 1
    assert src.steps == []
    assert ".partial" in text
    assert partial_path(out).read_text(encoding="utf-8") == "old"
    assert not out.exists()


def test_a_write_failure_exits_one_and_never_leaves_a_finished_file(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    out = tmp_path / "nq_1m.csv"
    bars = bars_between(START, LAST)
    bad = [*bars[:2], Bar("ES", bars[2].ts, D(1), D(1), D(1), D(1), 1), *bars[3:]]
    code, text = run(nq, Source(bad), out, Clock())
    assert code == 1
    assert "EXPORT FAILED:" in text.splitlines()[-1]
    assert "symbol" in text.splitlines()[-1]
    assert not out.exists()
    assert partial_path(out).exists()  # clearly named, never mistaken for a finished file


# --- config -----------------------------------------------------------------------------


def test_the_export_numbers_live_in_config() -> None:
    m = load_execution(REPO_ROOT / "config" / "execution.yaml").market_data
    assert m.export_wait_seconds >= 1
    assert m.export_max_missing_minutes >= 0


@pytest.mark.parametrize(
    ("key", "bad", "ok"),
    [("export_wait_seconds", 0, 1), ("export_max_missing_minutes", -1, 0)],
)
def test_an_export_number_below_its_minimum_is_refused(
    tmp_path: Path, key: str, bad: int, ok: int
) -> None:
    text = (REPO_ROOT / "config" / "execution.yaml").read_text()

    def with_value(value: int) -> Path:
        lines = [
            f"  {key}: {value}" if line.strip().startswith(f"{key}:") else line
            for line in text.splitlines()
        ]
        path = tmp_path / f"execution-{value}.yaml"
        path.write_text("\n".join(lines) + "\n")
        return path

    with pytest.raises(ConfigError, match=key):
        load_execution(with_value(bad))
    assert getattr(load_execution(with_value(ok)).market_data, key) == ok


# --- the backtest reads the export unchanged ----------------------------------------------


def test_the_backtest_runs_on_an_exported_file_unchanged(
    nq: InstrumentProfile, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Proves the file format only: synthetic bars say nothing about any result.
    from footprint.backtest.__main__ import main as backtest

    bars = bars_between(START, START + 6 * 60 * MIN)
    out = tmp_path / "nq_1m.csv"
    assert run(nq, Source(bars), out, Clock())[0] == 0
    run_dir = tmp_path / "run"
    argv = ["--bars", str(out), "--out", str(run_dir), "--config", str(REPO_ROOT / "config")]
    code = backtest([*argv, "--rolls", "none"])  # six hours of one contract: no roll (BH-9)
    capsys.readouterr()
    assert code in (0, 1)  # PASS / FAIL / NOT ENOUGH TRADES; 2 would be INVALID
    assert (run_dir / "readout.json").exists()
    assert str(out) in (run_dir / "readout.md").read_text(encoding="utf-8")


# --- --days and the window start (ibapi-free parts of `history`) ----------------------


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


def test_without_days_the_configured_history_is_exported(profiles: Profiles) -> None:
    assert with_days(profiles, None) is profiles


@pytest.mark.parametrize("days", [1, 7, 400])
def test_days_replaces_only_the_history_length(profiles: Profiles, days: int) -> None:
    changed = with_days(profiles, days)
    assert changed.execution.market_data.history_days == days
    assert (
        replace(
            changed.execution.market_data, history_days=profiles.execution.market_data.history_days
        )
        == profiles.execution.market_data
    )
    assert changed.instrument is profiles.instrument
    assert changed.risk is profiles.risk
    assert changed.strategy is profiles.strategy
    assert changed.execution.ibkr is profiles.execution.ibkr


@pytest.mark.parametrize("days", [0, -1])
def test_days_below_one_is_refused(profiles: Profiles, days: int) -> None:
    with pytest.raises(ValueError, match="--days"):
        with_days(profiles, days)


def test_the_window_starts_at_midnight_utc_that_many_days_before_today() -> None:
    now = datetime(2026, 10, 14, 0, 0, 30, tzinfo=UTC)
    assert window_start(now, 35) == datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    late = datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC)
    assert window_start(late, 1) == datetime(2026, 10, 13, 0, 0, tzinfo=UTC)
