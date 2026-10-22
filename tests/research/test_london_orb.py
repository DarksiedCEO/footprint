from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from footprint.core.config import ConfigError
from footprint.research import __main__ as cli
from footprint.research.bars import Day
from footprint.research.engine import DayState, run
from footprint.research.rules import RULES
from footprint.research.rules.london_orb import LondonOrb, OrbSettings, load_orb
from footprint.research.settings import ResearchSettings
from footprint.research.vol import VolIndex
from tests.conftest import REPO_ROOT
from tests.research.conftest import MINUTE, NY, make_day, ny

VOL = VolIndex((date(2024, 3, 4),), (20.0,), divisor=100.0, max_age_days=5)
FRESH = DayState(0, 0.0)
RANGE_HIGH, RANGE_LOW = 18010.0, 17990.0


def london(after: list[float], *, lead: list[float] | None = None) -> Day:
    """A London morning on 2024-03-05: optional `lead` bars just before 03:00, fifteen
    range bars 03:00-03:14 (close 18000, high 18010, low 17990), then one bar per close
    in `after`, each opening at the previous close (high/low = its open and close)."""
    lead = lead or []
    begin = ny(2024, 3, 5, 3, 0) - len(lead) * MINUTE
    range_closes = [18000.0] * 15
    closes = [*lead, *range_closes, *after]
    opens = [*lead, *range_closes]
    opens += [18000.0, *after[:-1]] if after else []
    tail = list(zip(opens[len(lead) + 15 :], after, strict=True))
    highs = [*lead, *([RANGE_HIGH] * 15), *(max(o, c) for o, c in tail)]
    lows = [*lead, *([RANGE_LOW] * 15), *(min(o, c) for o, c in tail)]
    return make_day(begin, closes, opens=opens, highs=highs, lows=lows, day=date(2024, 3, 5))


def orb(research: ResearchSettings, green: float = 500.0) -> LondonOrb:
    return LondonOrb(load_orb(research), green, research.zone)


def plain(research: ResearchSettings) -> ResearchSettings:
    return replace(research, option=replace(research.option, reference_price=None))


# --- settings ---------------------------------------------------------------------------


def test_pre_registered_settings_are_read(research: ResearchSettings) -> None:
    s = load_orb(research)
    assert s == OrbSettings(
        range_start=time(3, 0),
        range_end=time(3, 15),
        entry_start=time(3, 15),
        entry_end=time(7, 0),
        flat=time(8, 25),
        max_trades_per_session=2,
        green_stop_dollars=(500.0, 1000.0),
    )


def test_registered_with_one_variant_per_green_stop(research: ResearchSettings) -> None:
    assert "london_orb" in RULES
    assert RULES["london_orb"].variants(research) == ("500", "1000")
    rule = RULES["london_orb"].build(research, "1000")
    assert isinstance(rule, LondonOrb)
    assert rule.green_stop == 1000.0
    with pytest.raises(ValueError, match="green stop"):
        RULES["london_orb"].build(research, "750")
    with pytest.raises(ValueError, match="green stop"):
        RULES["london_orb"].build(research, None)


def _rules(research: ResearchSettings, **change: Any) -> ResearchSettings:
    body = dict(research.rules["london_orb"])  # type: ignore[call-overload]
    body.update(change)
    return replace(research, rules={"london_orb": body})


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"range_start": "3am"}, "range_start"),
        ({"flat": "08:25:30"}, "flat"),
        ({"range_end": "03:00"}, "range_end"),
        ({"entry_start": "03:14"}, "entry_start"),
        ({"entry_end": "03:15"}, "entry_end"),
        ({"flat": "07:00"}, "flat"),
        ({"max_trades_per_session": 0}, "max_trades_per_session"),
        ({"green_stop_dollars": []}, "green_stop_dollars"),
        ({"green_stop_dollars": [500, 0]}, "green_stop_dollars"),
        ({"green_stop_dollars": [500, 500]}, "green_stop_dollars"),
        ({"green_stop_dollars": 500}, "green_stop_dollars"),
        ({"extra": 1}, "unknown"),
    ],
)
def test_bad_settings_are_refused(
    research: ResearchSettings, change: dict[str, object], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        load_orb(_rules(research, **change))


def test_missing_rule_section_is_refused(research: ResearchSettings) -> None:
    with pytest.raises(ConfigError, match="london_orb"):
        load_orb(replace(research, rules={}))


# --- the range and the entry window -------------------------------------------------------


def test_close_above_the_range_buys_a_call_with_the_low_as_stop(
    research: ResearchSettings,
) -> None:
    day = london([18011.0])
    signal = orb(research).entry(day, 15, FRESH)
    assert signal is not None
    assert signal.direction == 1
    assert signal.futures_stop == RANGE_LOW
    assert signal.flat_by == ny(2024, 3, 5, 8, 25)


def test_close_below_the_range_buys_a_put_with_the_high_as_stop(
    research: ResearchSettings,
) -> None:
    signal = orb(research).entry(london([17989.0]), 15, FRESH)
    assert signal is not None
    assert signal.direction == -1
    assert signal.futures_stop == RANGE_HIGH


@pytest.mark.parametrize("close", [RANGE_HIGH, RANGE_LOW, 18000.0])
def test_a_close_on_or_inside_the_range_is_no_entry(
    research: ResearchSettings, close: float
) -> None:
    assert orb(research).entry(london([close]), 15, FRESH) is None


def test_range_bars_never_enter(research: ResearchSettings) -> None:
    day = london([18011.0])
    rule = orb(research)
    assert all(rule.entry(day, i, FRESH) is None for i in range(15))


def test_bars_before_the_range_are_not_in_it(research: ResearchSettings) -> None:
    """A 02:59 bar with a far higher high is not part of the range."""
    day = london([18011.0], lead=[18100.0])
    signal = orb(research).entry(day, 16, FRESH)
    assert signal is not None
    assert signal.futures_stop == RANGE_LOW


def test_the_trigger_bar_is_not_part_of_the_range(research: ResearchSettings) -> None:
    """The 03:15 bar's own high (above the range) does not widen it."""
    day = london([18050.0])
    assert day.high[15] == 18050.0
    assert orb(research).entry(day, 15, FRESH) is not None


def test_entry_window_edges(research: ResearchSettings) -> None:
    flat = [18000.0] * (4 * 60 - 16)  # inside the range from 03:15 to 06:58
    day = london([*flat, 18011.0, 18012.0])  # bars opening 06:59, then 07:00
    assert day.clock[-2] == time(6, 59)
    assert day.clock[-1] == time(7, 0)
    assert orb(research).entry(day, len(day) - 2, FRESH) is not None
    assert orb(research).entry(day, len(day) - 1, FRESH) is None


def test_entry_start_is_read_even_when_it_is_later_than_the_range_end(
    research: ResearchSettings,
) -> None:
    later = replace(load_orb(research), entry_start=time(3, 20))
    rule = LondonOrb(later, 500.0, research.zone)
    day = london([18011.0] * 6)  # outside the range from 03:15 on
    assert [rule.entry(day, i, FRESH) is not None for i in range(15, 21)] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]


def test_after_the_window_no_entry_even_outside_the_range(research: ResearchSettings) -> None:
    day = london([18000.0] * (4 * 60 - 15) + [18050.0] * 5)
    rule = orb(research)
    assert [rule.entry(day, i, FRESH) for i in range(15 + 4 * 60 - 15, len(day))] == [None] * 5


def test_a_session_missing_a_range_bar_is_not_traded(research: ResearchSettings) -> None:
    whole = london([18011.0])
    keep = [k for k in range(len(whole)) if k != 7]

    def pick(values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(values[k] for k in keep)

    gappy = Day(
        whole.day,
        pick(whole.ts),
        pick(whole.clock),
        pick(whole.open),
        pick(whole.high),
        pick(whole.low),
        pick(whole.close),
        pick(whole.volume),
    )
    assert orb(research).entry(gappy, len(gappy) - 1, FRESH) is None


def test_a_session_without_any_range_bar_is_not_traded(research: ResearchSettings) -> None:
    day = make_day(ny(2024, 3, 5, 3, 30), [18000.0, 18100.0])
    assert orb(research).entry(day, 1, FRESH) is None


def test_each_day_has_its_own_range(research: ResearchSettings) -> None:
    rule = orb(research)
    first = london([18011.0])
    second = make_day(
        ny(2024, 3, 6, 3, 0),
        [18500.0] * 15 + [18011.0],
        highs=[18510.0] * 15 + [18500.0],
        lows=[18490.0] * 15 + [18011.0],
        opens=[18500.0] * 15 + [18500.0],
    )
    assert rule.entry(first, 15, FRESH) is not None
    signal = rule.entry(second, 15, FRESH)
    assert signal is not None
    assert signal.direction == -1
    assert signal.futures_stop == 18510.0
    assert signal.flat_by == ny(2024, 3, 6, 8, 25)


# --- no lookahead -------------------------------------------------------------------------


def _cut(day: Day, end: int) -> Day:
    return Day(
        day.day,
        day.ts[:end],
        day.clock[:end],
        day.open[:end],
        day.high[:end],
        day.low[:end],
        day.close[:end],
        day.volume[:end],
    )


def test_no_lookahead_the_answer_at_a_bar_ignores_every_later_bar(
    research: ResearchSettings,
) -> None:
    moves = [18005.0, 18011.0, 17980.0, 18030.0, 17950.0, 18000.0, 18020.0]
    day = london(moves)
    for i in range(len(day)):
        full = orb(research).entry(day, i, FRESH)
        alone = orb(research).entry(_cut(day, i + 1), i, FRESH)
        assert full == alone, i


def test_no_lookahead_after_a_cached_range(research: ResearchSettings) -> None:
    """Asked on a truncated copy first, then on the full day, the range is the same."""
    rule = orb(research)
    day = london([18011.0, 18020.0])
    rule.entry(_cut(day, 16), 15, FRESH)
    assert rule.entry(day, 16, FRESH) == orb(research).entry(day, 16, FRESH)


# --- green stop, trade limit, flat time, one position ---------------------------------------


def test_green_stop_blocks_new_entries_at_or_above_it(research: ResearchSettings) -> None:
    day = london([18011.0])
    rule = orb(research, green=500.0)
    assert rule.entry(day, 15, DayState(1, 499.99)) is not None
    assert rule.entry(day, 15, DayState(1, 500.0)) is None
    assert rule.entry(day, 15, DayState(1, 900.0)) is None
    assert orb(research, green=1000.0).entry(day, 15, DayState(1, 900.0)) is not None
    assert rule.entry(day, 15, DayState(1, -2000.0)) is not None


def test_max_trades_per_session(research: ResearchSettings) -> None:
    day = london([18011.0])
    rule = orb(research)
    assert rule.entry(day, 15, DayState(1, 0.0)) is not None
    assert rule.entry(day, 15, DayState(2, 0.0)) is None
    one = LondonOrb(replace(load_orb(research), max_trades_per_session=1), 500.0, research.zone)
    assert one.entry(day, 15, DayState(1, 0.0)) is None


def _whipsaw() -> list[float]:
    """Breaks up at 03:15, stops out below the range low, breaks up again, falls back
    below the low (second stop), and breaks up a third time."""
    return [18011.0, 17985.0, 18000.0, 18012.0, 17985.0, 18000.0, 18013.0, 18020.0]


def test_two_trades_at_most_through_the_engine(research: ResearchSettings) -> None:
    trades, _ = run([london(_whipsaw())], orb(research), VOL, plain(research), "booker")
    assert len(trades) == 2
    assert [t.reasons for t in trades] == [("futures_stop",), ("futures_stop",)]
    assert trades[0].exit_ts <= trades[1].entry_ts  # one position at a time
    assert [t.entry_ts for t in trades] == [ny(2024, 3, 5, 3, 16), ny(2024, 3, 5, 3, 19)]


def test_the_rule_never_closes_an_open_trade(research: ResearchSettings) -> None:
    """The green stop only blocks NEW entries: the rule has no say over an open trade,
    which rides its own exit (here, to the end of the data)."""
    up = [18011.0 + 10 * k for k in range(8)]
    trades, _ = run([london(up)], orb(research, 500.0), VOL, plain(research), "booker")
    assert len(trades) == 1
    assert trades[0].reasons == ("end_of_day",)
    assert trades[0].net > 500  # past the green stop while open, and never closed by it


def test_green_stop_grid_changes_whether_a_second_trade_is_taken(
    research: ResearchSettings,
) -> None:
    # First trade: up 140 points, then a lock exit with a net between 500 and 1000;
    # the price stays above the range, so the next bar is a fresh break. With green 500
    # there is no second trade; with 1000 there is.
    path = [18011.0, 18060.0, 18110.0, 18150.0, 18120.0, 18090.0, 18080.0, 18070.0, 18060.0]
    s = plain(research)
    low, _ = run([london(path)], orb(research, 500.0), VOL, s, "booker")
    high, _ = run([london(path)], orb(research, 1000.0), VOL, s, "booker")
    assert 500 <= low[0].net < 1000, low[0].net
    assert low[0].reasons == ("lock",)
    assert len(low) == 1
    assert len(high) == 2
    assert high[0] == low[0]


def test_hard_flat_at_the_configured_time(research: ResearchSettings) -> None:
    to_flat = [18011.0] + [18012.0] * (5 * 60 + 15)  # 03:15 to 08:30
    trades, _ = run([london(to_flat)], orb(research), VOL, plain(research), "booker")
    assert len(trades) == 1
    assert trades[0].reasons == ("flat",)
    assert trades[0].exit_ts == ny(2024, 3, 5, 8, 25)
    assert trades[0].session == "london"


def test_scale_out_runs_two_contracts(research: ResearchSettings) -> None:
    up = [18011.0 + 15 * k for k in range(12)]
    (t,) = run([london(up)], orb(research), VOL, plain(research), "scale_out")[0]
    assert t.contracts == 2
    assert t.reasons[0] == "target"


# --- CLI --------------------------------------------------------------------------------


def test_cli_runs_the_rule_with_a_variant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = []
    for m in range(20):
        p = 18000 if m < 15 else 18011 + m
        rows.append(f"2024-03-05T03:{m:02d}:00-05:00,{p},{p + 1},{p - 1},{p},5")
    bars = tmp_path / "bars.csv"
    bars.write_text("ts,open,high,low,close,volume\n" + "\n".join(rows) + "\n")
    vol = tmp_path / "VXN_History.csv"
    vol.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n03/04/2024,1,1,1,20\n")
    args = ["--bars", str(bars), "--vol", str(vol), "--rule", "london_orb"]
    args += ["--config", str(REPO_ROOT / "config")]
    assert cli.main([*args, "--variant", "1000", "--exit", "scale_out"]) == 0
    out = capsys.readouterr().out
    assert "rule: london_orb (variant 1000)" in out
    assert "exit: scale_out" in out
    assert "trades: 1   skipped (no usable vol): 0" in out
    with pytest.raises(SystemExit):
        cli.main([*args, "--variant", "750"])
    with pytest.raises(SystemExit):
        cli.main(args)


def test_flat_time_is_on_the_session_day(research: ResearchSettings) -> None:
    signal = orb(research).entry(london([18011.0]), 15, FRESH)
    assert signal is not None
    assert signal.flat_by.tzinfo is not None
    assert signal.flat_by == datetime(2024, 3, 5, 8, 25, tzinfo=NY)


# --- review round 1 (38-M1, 38-M2, 38-I1) ---------------------------------------------------


def test_a_wick_above_the_range_that_closes_inside_is_no_entry(
    research: ResearchSettings,
) -> None:
    """38-M1: only the CLOSE counts. A trigger bar poking above the high (or below the
    low) and closing inside the range does not enter."""
    up = london([18005.0])
    up_high = list(up.high)
    up_high[15] = 18030.0
    poke_up = replace(up, high=tuple(up_high))
    assert orb(research).entry(poke_up, 15, FRESH) is None
    down = london([17995.0])
    down_low = list(down.low)
    down_low[15] = 17970.0
    poke_down = replace(down, low=tuple(down_low))
    assert orb(research).entry(poke_down, 15, FRESH) is None


def _without(day: Day, drop: int) -> Day:
    keep = [k for k in range(len(day)) if k != drop]

    def pick(values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(values[k] for k in keep)

    return Day(
        day.day,
        pick(day.ts),
        pick(day.clock),
        pick(day.open),
        pick(day.high),
        pick(day.low),
        pick(day.close),
        pick(day.volume),
    )


def _on(day: Day, when: date) -> Day:
    shift = datetime.combine(when, time()) - datetime.combine(day.day, time())
    return replace(day, day=when, ts=tuple(t + shift for t in day.ts))


def test_report_counts_sessions_without_a_full_range_per_year(
    research: ResearchSettings,
) -> None:
    """38-M2: the sessions the rule cannot trade (a missing range minute) are counted,
    per year, so the sample selection is visible."""
    full = london([18011.0])
    days = [
        _on(full, date(2010, 3, 2)),
        _on(_without(full, 3), date(2010, 3, 3)),
        _on(_without(full, 14), date(2010, 3, 4)),
        _on(full, date(2011, 3, 2)),
        _on(make_day(ny(2011, 3, 3, 4, 0), [18000.0]), date(2011, 3, 3)),
    ]
    lines = orb(research).report(days)
    assert "sessions without a full 03:00-03:15 range (not traded): 3 of 5" in lines
    assert "  2010: 2 of 3" in lines
    assert "  2011: 1 of 2" in lines


def test_report_states_the_clock_change_caveat(research: ResearchSettings) -> None:
    """38-I1."""
    text = "\n".join(orb(research).report([]))
    assert "US and UK clocks change on different dates" in text
    assert "sessions without a full 03:00-03:15 range (not traded): 0 of 0" in text


def test_cli_prints_the_rule_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        f"2024-03-05T03:{m:02d}:00-05:00,18000,18001,17999,18000,5" for m in range(15) if m != 4
    ]
    bars = tmp_path / "bars.csv"
    bars.write_text("ts,open,high,low,close,volume\n" + "\n".join(rows) + "\n")
    vol = tmp_path / "VXN_History.csv"
    vol.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n03/04/2024,1,1,1,20\n")
    args = ["--bars", str(bars), "--vol", str(vol), "--rule", "london_orb", "--variant", "500"]
    assert cli.main([*args, "--config", str(REPO_ROOT / "config")]) == 0
    out = capsys.readouterr().out
    assert "sessions without a full 03:00-03:15 range (not traded): 1 of 1" in out
    assert "  2024: 1 of 1" in out
    assert "US and UK clocks change on different dates" in out
