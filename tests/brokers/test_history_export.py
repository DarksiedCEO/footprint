"""Q2: the `history` export driver. It steps MarketData (read-only) until the whole
history has loaded, refuses a history with a gap, and only then writes the CSV the
backtest reads. A timeout, a gap or a write failure exits 1 and leaves no file under the
finished name. Nothing here needs ibapi."""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.brokers.ibkr.history_export import export_history
from footprint.core.config import ConfigError, InstrumentProfile, load_execution, load_instrument
from footprint.core.models import Bar
from footprint.data.csv_export import partial_path
from footprint.data.file_source import CsvBarSource
from tests.conftest import REPO_ROOT

MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)
NOW = datetime(2026, 10, 6, 16, 0, 7, tzinfo=UTC)  # a Tuesday, 09:00 PT
START = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
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
    source: Source,
    out: Path,
    clock: Clock,
    *,
    wait: float = 60,
    max_missing: int = 0,
    poll: float = 1,
) -> tuple[int, str]:
    text = io.StringIO()
    code = export_history(
        source,
        out,
        START,
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
    code, _ = run(nq, Source(bars_between(START, START + MIN), ready_at=2), out, clock, wait=2)
    assert code == 0
    assert clock.slept == [1, 1]


def test_a_wait_of_zero_still_looks_once(nq: InstrumentProfile, tmp_path: Path) -> None:
    out = tmp_path / "nq_1m.csv"
    clock = Clock()
    src = Source(bars_between(START, START + MIN))
    code, _ = run(nq, src, out, clock, wait=0)
    assert code == 0
    assert len(src.steps) == 1
    assert clock.slept == []


def test_a_history_with_a_gap_is_refused_and_nothing_is_written(
    nq: InstrumentProfile, tmp_path: Path
) -> None:
    hole = frozenset(START + (10 + i) * MIN for i in range(3))
    out = tmp_path / "nq_1m.csv"
    code, text = run(
        nq, Source(bars_between(START, START + 30 * MIN, hole)), out, Clock(), max_missing=2
    )
    assert code == 1
    last = text.splitlines()[-1]
    assert last.startswith("REFUSED: the history has a gap: 3 tradable minute(s) from ")
    assert last.endswith("; nothing written")
    assert not out.exists()
    assert not partial_path(out).exists()


def test_the_gap_allowance_is_the_configured_number(nq: InstrumentProfile, tmp_path: Path) -> None:
    hole = frozenset(START + (10 + i) * MIN for i in range(3))
    out = tmp_path / "nq_1m.csv"
    code, _ = run(
        nq, Source(bars_between(START, START + 30 * MIN, hole)), out, Clock(), max_missing=3
    )
    assert code == 0
    assert len(list(CsvBarSource(out, "NQ").bars())) == 28


def test_a_missing_start_is_a_gap(nq: InstrumentProfile, tmp_path: Path) -> None:
    out = tmp_path / "nq_1m.csv"
    code, text = run(nq, Source(bars_between(START + 5 * MIN, START + 30 * MIN)), out, Clock())
    assert code == 1
    assert "5 tradable minute(s) from 2026-10-06T15:00:00+00:00" in text
    assert not out.exists()


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
    bars = bars_between(START, START + 3 * MIN)
    bad = [*bars[:2], Bar("ES", bars[2].ts, D(1), D(1), D(1), D(1), 1), bars[3]]
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
