"""Q3 slice 2b: the recorder loop (`record_chain`), without ibapi. A snapshot every
interval, for the run length; a disconnect, an empty chain, a refused or failed write is
said (an ERROR names the file) and the recording keeps going; nothing guessed or stale
is ever written."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.brokers.ibkr.chain_recorder import record_chain, run_seconds
from footprint.brokers.ibkr.market import RecordedChain, RecordedOption
from footprint.core.config import ConfigError, load_execution
from footprint.core.options import Right
from footprint.data.chain_record import ChainRow, day_file, write_snapshot
from footprint.execution.orders import Quote

D = Decimal
START = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_AGE = 5


class Clock:
    def __init__(self, at: datetime = START) -> None:
        self.at = at
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.at += timedelta(seconds=seconds)


def chain_at(now: datetime, age: float = 1) -> RecordedChain:
    stamp = now - timedelta(seconds=age)
    return RecordedChain(
        future=Quote("NQ-FUT", D("20025.00"), D("20025.50"), stamp),
        options=(
            RecordedOption(
                Quote("NQ-C-20020", D(150), D(151), stamp),
                date(2026, 10, 16),
                D(20020),
                Right.CALL,
                1234,
            ),
        ),
    )


class Market:
    """MarketData as the recorder reads it: `step`, then `recorded_chain`."""

    def __init__(self, answer: Callable[[datetime], RecordedChain | None]) -> None:
        self.answer = answer
        self.steps: list[datetime] = []
        self.reads: list[datetime] = []

    def step(self, now: datetime) -> None:
        self.steps.append(now)

    def recorded_chain(self, now: datetime) -> RecordedChain | None:
        self.reads.append(now)
        return self.answer(now)


def run(
    market: Market,
    directory: Path,
    clock: Clock,
    *,
    interval: int = 60,
    run_seconds: int = 180,
    poll: float = 1,
) -> tuple[int, list[str]]:
    said: list[str] = []
    code = record_chain(
        market,
        directory,
        interval_seconds=interval,
        run_seconds=run_seconds,
        max_age_seconds=MAX_AGE,
        poll_seconds=poll,
        say=said.append,
        clock=clock,
        sleep=clock.sleep,
    )
    return code, said


def rows_in(directory: Path) -> list[str]:
    return day_file(directory, START).read_text().splitlines()[1:]


def test_one_snapshot_each_interval_for_the_run_length(tmp_path: Path) -> None:
    clock = Clock()
    market = Market(chain_at)
    code, said = run(market, tmp_path, clock)
    assert code == 0
    assert market.reads == [START + timedelta(seconds=s) for s in (0, 60, 120)]
    assert [r.split(",")[0] for r in rows_in(tmp_path)] == [t.isoformat() for t in market.reads]
    assert clock.at == START + timedelta(seconds=180)  # stops at the end, not after
    assert "3 snapshots written" in said[-1]


def test_market_data_is_stepped_every_poll_between_snapshots(tmp_path: Path) -> None:
    clock = Clock()
    market = Market(chain_at)
    run(market, tmp_path, clock, interval=10, run_seconds=10, poll=2)
    assert market.steps == [START + timedelta(seconds=s) for s in (0, 2, 4, 6, 8)]
    assert all(s <= 2 for s in clock.sleeps)


def test_a_sleep_never_runs_past_the_next_snapshot_or_the_end(tmp_path: Path) -> None:
    clock = Clock()
    market = Market(chain_at)
    run(market, tmp_path, clock, interval=7, run_seconds=10, poll=5)
    assert market.reads == [START, START + timedelta(seconds=7)]
    assert clock.sleeps == [5, 2, 3]
    assert clock.at == START + timedelta(seconds=10)


def test_the_snapshot_is_read_after_the_step_at_a_fresh_time(tmp_path: Path) -> None:
    """Quotes stamped while `step` ran must not be later than the snapshot."""
    clock = Clock()

    class Slow(Market):
        def step(self, now: datetime) -> None:
            super().step(now)
            clock.at += timedelta(milliseconds=300)

    market = Slow(lambda now: chain_at(now, age=0))
    code, _ = run(market, tmp_path, clock, interval=60, run_seconds=60)
    assert code == 0
    assert market.reads == [START + timedelta(milliseconds=300)]
    assert len(rows_in(tmp_path)) == 1


def test_missed_snapshots_are_skipped_never_backfilled(tmp_path: Path) -> None:
    clock = Clock()

    class Stuck(Market):
        def step(self, now: datetime) -> None:
            super().step(now)
            if len(self.steps) == 1:
                clock.at += timedelta(seconds=130)  # one step took more than two intervals

    market = Stuck(chain_at)
    code, _ = run(market, tmp_path, clock, interval=60, run_seconds=200)
    assert code == 0
    assert market.reads == [START + timedelta(seconds=130), START + timedelta(seconds=180)]
    assert len(rows_in(tmp_path)) == 2


def test_no_chain_is_said_and_the_recording_keeps_going(tmp_path: Path) -> None:
    """A disconnect (or no fresh quote) gives no chain: nothing is written for it."""
    clock = Clock()
    gone = START + timedelta(seconds=60)
    market = Market(lambda now: None if now == gone else chain_at(now))
    code, said = run(market, tmp_path, clock)
    assert code == 0
    assert len(rows_in(tmp_path)) == 2
    assert any(f"no fresh chain at {gone.isoformat()}" in s for s in said)
    assert "1 with no fresh chain" in said[-1]


def test_only_no_chain_writes_nothing_and_fails(tmp_path: Path) -> None:
    code, said = run(Market(lambda now: None), tmp_path, Clock())
    assert code == 1
    assert not day_file(tmp_path, START).exists()
    assert "0 snapshots written" in said[-1]


def test_a_stale_quote_is_refused_said_as_an_error_and_the_recording_goes_on(
    tmp_path: Path,
) -> None:
    clock = Clock()
    stale = START + timedelta(seconds=60)
    market = Market(lambda now: chain_at(now, age=MAX_AGE + 1 if now == stale else 1))
    code, said = run(market, tmp_path, clock)
    assert code == 1  # a refused snapshot is not a clean recording
    assert [r.split(",")[0] for r in rows_in(tmp_path)] == [
        START.isoformat(),
        (START + timedelta(seconds=120)).isoformat(),
    ]
    errors = [s for s in said if s.startswith("ERROR")]
    assert len(errors) == 1
    assert stale.isoformat() in errors[0]
    assert "older than" in errors[0]
    assert "1 refused" in said[-1]


def test_rows_that_cannot_be_built_are_refused_and_the_recording_goes_on(
    tmp_path: Path,
) -> None:
    """G31-2: `snapshot_rows` raises for an out-of-range line (e.g. negative OI)."""
    clock = Clock()
    bad = START + timedelta(seconds=60)

    def answer(now: datetime) -> RecordedChain:
        chain = chain_at(now)
        if now != bad:
            return chain
        option = chain.options[0]
        return RecordedChain(
            chain.future,
            (RecordedOption(option.quote, option.expiry, option.strike, option.right, -1),),
        )

    code, said = run(Market(answer), tmp_path, clock)
    assert code == 1
    assert len(rows_in(tmp_path)) == 2
    assert sum(s.startswith("ERROR") for s in said) == 1


def test_a_torn_day_file_is_an_error_naming_it_every_interval(tmp_path: Path) -> None:
    """G31-4: the file is kept for a person; each interval says so again."""
    path = day_file(tmp_path, START)
    run(Market(chain_at), tmp_path, Clock(), run_seconds=60)
    path.write_text(path.read_text().rstrip("\n"))  # a write cut off mid-line
    before = path.read_text()
    code, said = run(Market(chain_at), tmp_path, Clock(START + timedelta(minutes=5)))
    assert code == 1
    assert path.read_text() == before  # never appended to
    errors = [s for s in said if s.startswith("ERROR")]
    assert len(errors) == 3
    assert all(str(path) in e for e in errors)


def test_a_file_system_error_is_said_and_the_recording_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def flaky(directory: Path, rows: Sequence[ChainRow], max_age_seconds: int) -> Path:
        calls.append(max_age_seconds)
        if len(calls) == 2:
            raise OSError("No space left on device")
        return write_snapshot(directory, rows, max_age_seconds)

    monkeypatch.setattr("footprint.brokers.ibkr.chain_recorder.write_snapshot", flaky)
    code, said = run(Market(chain_at), tmp_path, Clock())
    assert code == 1
    assert calls == [MAX_AGE] * 3  # the configured age limit reaches the writer
    assert len(rows_in(tmp_path)) == 2
    assert any(s.startswith("ERROR") and "No space left" in s for s in said)
    assert "1 failed" in said[-1]


def test_a_missing_directory_is_refused_before_anything_runs(tmp_path: Path) -> None:
    market = Market(chain_at)
    code, said = run(market, tmp_path / "nope", Clock())
    assert code == 1
    assert market.steps == []
    assert said[-1].startswith("REFUSED")


@pytest.mark.parametrize(
    ("interval", "run_seconds", "poll"),
    [(0, 60, 1), (60, 0, 1), (60, 60, 0)],
)
def test_a_non_positive_number_is_refused(
    tmp_path: Path, interval: int, run_seconds: int, poll: float
) -> None:
    class NoSpin(Clock):
        def sleep(self, seconds: float) -> None:
            if seconds <= 0:
                raise RuntimeError("a zero sleep: the loop would spin")  # fail, never hang
            super().sleep(seconds)

    with pytest.raises(ValueError, match="must be positive"):
        run(
            Market(chain_at),
            tmp_path,
            NoSpin(),
            interval=interval,
            run_seconds=run_seconds,
            poll=poll,
        )


def test_a_naive_clock_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"clock\(\)"):
        run(Market(chain_at), tmp_path, Clock(START.replace(tzinfo=None)))


# --- config -----------------------------------------------------------------------------


def test_the_recorder_numbers_live_in_config() -> None:
    m = load_execution(REPO_ROOT / "config" / "execution.yaml").market_data
    assert m.record_interval_seconds >= 1
    assert m.record_minutes >= 1


@pytest.mark.parametrize("key", ["record_interval_seconds", "record_minutes"])
def test_a_recorder_number_below_one_is_refused(tmp_path: Path, key: str) -> None:
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
        load_execution(with_value(0))
    assert getattr(load_execution(with_value(1)).market_data, key) == 1


# --- the run length (`--minutes`) -------------------------------------------------------


def test_the_run_length_is_the_configured_minutes_unless_given() -> None:
    assert run_seconds(60, None) == 3600
    assert run_seconds(60, 5) == 300
    assert run_seconds(60, 1) == 60


@pytest.mark.parametrize("minutes", [0, -1])
def test_minutes_below_one_are_refused(minutes: int) -> None:
    with pytest.raises(ValueError, match=f"--minutes {minutes}"):
        run_seconds(60, minutes)
