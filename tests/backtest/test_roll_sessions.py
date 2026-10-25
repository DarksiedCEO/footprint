"""No new entry on the first session after a contract roll (backtest only; review round 1,
M1; decision BH-9).

The history is a RAW front-month series: at a roll the price jumps from one contract to
the next at a session open (+112 to +259 points from 2022-09 to 2024-09, 1.6 to 2.4 N at
the data's level, measured on the roll sessions' first bar). The readers' look-back then
mixes the two contracts (prior-day levels, VWAP side, multi-session averages), enough
to flip the levels room. Fail closed: with `rolls.block_first_session` (DEFAULT, draft,
Andre BH-9) no entry is made on the trading day the first session on a new contract
belongs to, and a run must say which sessions those are (`--rolls FILE`, or
`--rolls none` for bars with no roll); without that it is refused. The alternative
(back-adjust the look-back across the splice) is recorded for Andre, not built.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.__main__ import main
from footprint.backtest.engine import Backtest
from footprint.backtest.model_check import BacktestRefused
from footprint.backtest.rolls import RollConsole, load_roll_sessions, roll_console
from footprint.backtest.settings import BacktestSettings, load_backtest
from footprint.core.config import ConfigError, Profiles
from footprint.core.models import Bar, Read, Score
from footprint.decision.console.desmond import ConsoleInputs, ConsoleVerdict, Gate
from tests.backtest.conftest import TRADE_FROM, config_copy, extend, workdir, write_csv
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME, LAST_DAY

D = Decimal


def blocking(settings: BacktestSettings, on: bool = True) -> BacktestSettings:
    return replace(settings, rolls=replace(settings.rolls, block_first_session=on))


def bars(history: tuple[Bar, ...]) -> list[Bar]:
    return extend(history, [D(-10)] * 15)  # one trade, stopped out


# --- settings ----------------------------------------------------------------------------


def test_the_shipped_default_blocks_and_is_a_draft() -> None:
    settings = load_backtest(REPO_ROOT / "config" / "backtest.yaml")
    assert settings.rolls.block_first_session is True
    text = (REPO_ROOT / "config" / "backtest.yaml").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if "block_first_session:" in x)
    assert "DEFAULT (draft, Andre BH-9)" in line


@pytest.mark.parametrize("value", ["1", "yes_please", "null", "[true]"])
def test_a_bad_switch_is_refused(tmp_path: Path, value: str) -> None:
    path = config_copy(tmp_path / "c", None, block_rolls=True) / "backtest.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("block_first_session: true", f"block_first_session: {value}"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="block_first_session"):
        load_backtest(path)


def test_the_switch_can_be_turned_off(tmp_path: Path) -> None:
    path = config_copy(tmp_path / "c", None, block_rolls=False) / "backtest.yaml"
    assert load_backtest(path).rolls.block_first_session is False


# --- the roll list -----------------------------------------------------------------------


def test_none_declares_no_roll() -> None:
    assert load_roll_sessions("none") == frozenset()


def test_a_roll_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "rolls.csv"
    path.write_text("session\n2024-06-17\n2024-09-16\n", encoding="utf-8")
    assert load_roll_sessions(str(path)) == {date(2024, 6, 17), date(2024, 9, 16)}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("day\n2024-06-17\n", "header"),
        ("session\n2024-06-17x\n", "line 2"),
        ("session\n2024-06-17\n2024-06-17\n", "twice"),
        ("session\n2024-06-15\n", "not a weekday"),
        ("session\n2024-09-16\n2024-06-17\n", "order"),
    ],
)
def test_a_bad_roll_file_is_refused(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "rolls.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_roll_sessions(str(path))


def test_a_missing_roll_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        load_roll_sessions(str(tmp_path / "nope.csv"))


# --- the console -------------------------------------------------------------------------


class Go:
    """A stand-in console that always says go (long), with one passing gate."""

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict:
        from footprint.core.calendar import SessionKind
        from footprint.core.models import Direction

        gate = Gate("prime_session", True, "PRIME")
        return ConsoleVerdict(inputs.as_of, SessionKind.PRIME, Direction.LONG, (gate,), (), 3, 3)


def _inputs(at: object) -> ConsoleInputs:
    read = Read("trend", DECISION_TIME, Score.NEUTRAL, "x")
    return ConsoleInputs(at, read, read, read, None, None, None, D(1))  # type: ignore[arg-type]


def test_on_a_roll_session_the_console_says_no(profiles: Profiles) -> None:
    console = RollConsole(Go(), frozenset({LAST_DAY}), profiles.instrument)
    verdict = console.evaluate(_inputs(DECISION_TIME))
    assert not verdict.go
    (failed,) = verdict.failed
    assert failed.name == "roll_session"
    assert str(LAST_DAY) in failed.detail
    assert verdict.gates[0] == Gate("prime_session", True, "PRIME")  # the inner gates kept


def test_on_any_other_session_the_verdict_is_the_inner_one(profiles: Profiles) -> None:
    console = RollConsole(Go(), frozenset({date(2026, 9, 14)}), profiles.instrument)
    assert console.evaluate(_inputs(DECISION_TIME)) == Go().evaluate(_inputs(DECISION_TIME))


def test_a_closed_moment_is_never_a_roll_session(profiles: Profiles) -> None:
    from datetime import UTC, datetime

    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)  # 05:00 PT Sunday: closed
    console = RollConsole(Go(), frozenset({date(2026, 9, 13)}), profiles.instrument)
    assert console.evaluate(_inputs(sunday)).go


def test_no_roll_listed_keeps_the_console(profiles: Profiles) -> None:
    inner = Go()
    assert roll_console(inner, profiles, frozenset()) is inner
    assert roll_console(None, profiles, frozenset()) is None
    wrapped = roll_console(None, profiles, frozenset({LAST_DAY}))
    assert isinstance(wrapped, RollConsole)
    assert type(wrapped.inner).__name__ == "Console"  # the live console, Desmond


# --- the engine --------------------------------------------------------------------------


def test_blocking_with_no_roll_list_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    with pytest.raises(BacktestRefused, match="--rolls"):
        Backtest(
            profiles, blocking(settings), bars(history), workdir(tmp_path), trade_from=TRADE_FROM
        )


def test_no_entry_on_a_roll_session(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    s = blocking(settings)
    free = Backtest(
        profiles,
        s,
        bars(history),
        workdir(tmp_path / "a"),
        trade_from=TRADE_FROM,
        roll_sessions=frozenset(),
    )
    assert len(free.run().trades) == 1
    rolled = Backtest(
        profiles,
        s,
        bars(history),
        workdir(tmp_path / "b"),
        trade_from=TRADE_FROM,
        roll_sessions=frozenset({LAST_DAY}),
    )
    result = rolled.run()
    assert result.trades == []
    assert result.fired == 0
    reasons = [g for d in rolled.journal.decisions() for g in d["gates"] if not g["passed"]]
    assert any(g["name"] == "roll_session" for g in reasons)


def test_a_roll_on_another_day_changes_nothing(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    other = frozenset({date(2026, 9, 14)})  # the day before: its session is look-back here
    base = Backtest(
        profiles, settings, bars(history), workdir(tmp_path / "a"), trade_from=TRADE_FROM
    ).run()
    rolled = Backtest(
        profiles,
        blocking(settings),
        bars(history),
        workdir(tmp_path / "b"),
        trade_from=TRADE_FROM,
        roll_sessions=other,
    ).run()
    keys = ("exit_reason", "entry_price", "exit_price", "net_pnl")
    assert [[t[k] for k in keys] for t in rolled.trades] == [
        [t[k] for k in keys] for t in base.trades
    ]


def test_with_blocking_off_a_roll_list_is_ignored(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    result = Backtest(
        profiles,
        blocking(settings, False),
        bars(history),
        workdir(tmp_path),
        trade_from=TRADE_FROM,
        roll_sessions=frozenset({LAST_DAY}),
    ).run()
    assert len(result.trades) == 1


# --- the command line --------------------------------------------------------------------


def _cli(tmp_path: Path, history: tuple[Bar, ...], *extra: str) -> tuple[int, Path]:
    path = tmp_path / "bars.csv"
    write_csv(path, bars(history))
    out = tmp_path / "run"
    config = config_copy(tmp_path / "config", None, block_rolls=True)
    argv = ["--bars", str(path), "--out", str(out), "--config", str(config)]
    with contextlib.redirect_stdout(io.StringIO()):
        code = main([*argv, "--trade-from", TRADE_FROM.isoformat(), *extra])
    return code, out


def test_the_command_line_refuses_blocking_without_a_roll_list(
    history: tuple[Bar, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _cli(tmp_path, history)
    assert code == 2
    assert "--rolls" in capsys.readouterr().err
    assert not (out / "readout.json").exists()


def test_the_command_line_blocks_the_listed_session(
    history: tuple[Bar, ...], tmp_path: Path
) -> None:
    rolls = tmp_path / "rolls.csv"
    rolls.write_text(f"session\n{LAST_DAY}\n", encoding="utf-8")
    code, out = _cli(tmp_path, history, "--rolls", str(rolls))
    assert code == 1
    readout = json.loads((out / "readout.json").read_text())
    assert readout["whole"]["trades"] == 0
    assert "1 listed" in readout["assumptions"]["roll sessions"]


def test_the_command_line_runs_with_no_roll_declared(
    history: tuple[Bar, ...], tmp_path: Path
) -> None:
    code, out = _cli(tmp_path, history, "--rolls", "none")
    assert code == 1
    readout = json.loads((out / "readout.json").read_text())
    assert readout["whole"]["trades"] == 1
    assert "none declared" in readout["assumptions"]["roll sessions"]


def test_a_bad_roll_file_is_refused_with_exit_2(
    history: tuple[Bar, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rolls = tmp_path / "rolls.csv"
    rolls.write_text("session\nnot-a-date\n", encoding="utf-8")
    code, _ = _cli(tmp_path, history, "--rolls", str(rolls))
    assert code == 2
    assert "rolls" in capsys.readouterr().err


def test_with_blocking_off_the_readout_says_nothing_of_rolls(
    history: tuple[Bar, ...], tmp_path: Path
) -> None:
    path = tmp_path / "bars.csv"
    write_csv(path, bars(history))
    out = tmp_path / "run"
    config = config_copy(tmp_path / "config", None, block_rolls=False)
    rolls = tmp_path / "rolls.csv"
    rolls.write_text(f"session\n{LAST_DAY}\n", encoding="utf-8")
    argv = ["--bars", str(path), "--out", str(out), "--config", str(config)]
    with contextlib.redirect_stdout(io.StringIO()):
        main([*argv, "--trade-from", TRADE_FROM.isoformat(), "--rolls", str(rolls)])
    readout = json.loads((out / "readout.json").read_text())
    assert readout["whole"]["trades"] == 1  # not blocked
    assert "roll sessions" not in readout["assumptions"]


# --- review round 2: R2-3 (the list must fit the bars), R2-4 (a roll on a closed day) ------


@pytest.mark.parametrize(
    "outside",
    [date(2026, 8, 7), date(2026, 9, 16)],  # before the first bar's day; after the last
)
def test_a_roll_session_outside_the_bars_is_refused(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    outside: date,
) -> None:
    sessions = frozenset({LAST_DAY, outside})
    with pytest.raises(BacktestRefused, match=str(outside)):
        Backtest(
            profiles,
            blocking(settings),
            bars(history),
            workdir(tmp_path),
            trade_from=TRADE_FROM,
            roll_sessions=sessions,
        )


def test_roll_sessions_on_the_bars_first_and_last_days_are_accepted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    from footprint.core.calendar import local_minute

    b = bars(history)
    session = profiles.instrument.prime_session
    first, last = local_minute(b[0].ts, session)[0], local_minute(b[-1].ts, session)[0]
    Backtest(
        profiles,
        blocking(settings),
        b,
        workdir(tmp_path),
        trade_from=TRADE_FROM,
        roll_sessions=frozenset({first, last}),
    )


def test_with_blocking_off_a_list_outside_the_bars_is_ignored(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    Backtest(
        profiles,
        blocking(settings, False),
        bars(history),
        workdir(tmp_path),
        trade_from=TRADE_FROM,
        roll_sessions=frozenset({date(2010, 6, 11)}),
    )


@pytest.mark.parametrize(
    ("roll", "blocked"),
    [
        (date(2026, 9, 15), date(2026, 9, 15)),  # a trading day: itself
        (date(2026, 9, 7), date(2026, 9, 8)),  # Labor Day, a no-trade day: the next day
        (date(2026, 12, 25), date(2026, 12, 28)),  # Christmas (Fri), a holiday: Monday
        (date(2026, 11, 26), date(2026, 11, 27)),  # Thanksgiving: the half day after it
    ],
)
def test_a_roll_on_a_closed_day_blocks_the_next_tradable_day(
    profiles: Profiles, roll: date, blocked: date
) -> None:
    console = RollConsole(Go(), frozenset({roll}), profiles.instrument)
    assert console.days == frozenset({blocked})


def test_the_moved_block_is_named_in_the_gate(profiles: Profiles) -> None:
    from datetime import UTC, datetime

    console = RollConsole(Go(), frozenset({date(2026, 9, 7)}), profiles.instrument)
    at = datetime(2026, 9, 8, 14, 1, tzinfo=UTC)  # 07:01 PT on Tuesday 2026-09-08
    (failed,) = console.evaluate(_inputs(at)).failed
    assert failed.name == "roll_session"
    assert "2026-09-08" in failed.detail
    assert "2026-09-07" in failed.detail  # the roll session it stands for


def test_a_roll_with_no_tradable_day_after_it_is_refused(profiles: Profiles) -> None:
    through = profiles.instrument.calendar_through
    from datetime import timedelta

    with pytest.raises(ValueError, match="no tradable day"):
        RollConsole(Go(), frozenset({through + timedelta(days=1)}), profiles.instrument)


def test_the_readout_names_the_first_and_last_roll(
    history: tuple[Bar, ...], tmp_path: Path
) -> None:
    rolls = tmp_path / "rolls.csv"
    rolls.write_text(f"session\n2026-09-14\n{LAST_DAY}\n", encoding="utf-8")
    code, out = _cli(tmp_path, history, "--rolls", str(rolls))
    assert code == 1
    line = json.loads((out / "readout.json").read_text())["assumptions"]["roll sessions"]
    assert "2 listed" in line
    assert "first 2026-09-14" in line
    assert f"last {LAST_DAY}" in line
