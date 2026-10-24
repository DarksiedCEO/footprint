"""The noise-band rule (noiseband-es-v1): settings, sigma, bands, checkpoints, the keep and
warm-up rules, first signal only, next-bar-open entry, flat, DST, and parity with the
source rule (tournament.py) on synthetic data."""

import csv
import random
import statistics
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from footprint.core.black76 import Right76, price
from footprint.core.config import ConfigError
from footprint.research.bars import Day, load_days
from footprint.research.engine import DayState, run
from footprint.research.rules import RULES
from footprint.research.rules.noise_band import NoiseBand, NoiseSettings, build_noise, load_noise
from footprint.research.settings import ResearchSettings, load_research
from footprint.research.vol import VolIndex
from tests.conftest import REPO_ROOT
from tests.research import tournament_reference as ref
from tests.research.conftest import MINUTE, NY, make_day

FRESH = DayState(0, 0.0)
VOL = VolIndex((date(2024, 1, 1),), (20.0,), divisor=100.0, max_age_days=400)
RTH_BARS = 390


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def rth(
    d: date,
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    first_minute: int = 0,
) -> Day:
    """Bars one a minute from 09:30 + first_minute (New York) on day d."""
    start = datetime.combine(d, time(9, 30), tzinfo=NY) + first_minute * MINUTE
    return make_day(start, closes, opens=opens, highs=highs, lows=lows, day=d)


def history(dates: list[date]) -> list[Day]:
    """14 kept days, each opening at 100 and closing every minute at 100 + a (a = +1 on
    even days, -1 on odd ones): sigma is 0.01 at every minute; each day's range is 1."""
    days = []
    for h, d in enumerate(dates):
        a = 1.0 if h % 2 == 0 else -1.0
        closes = [100.0 + a] * RTH_BARS
        days.append(rth(d, closes, opens=[100.0, *closes[:-1]]))
    return days


def today(d: date, closes: list[float], **kw: list[float]) -> Day:
    return rth(d, closes, opens=kw.pop("opens", [100.0, *closes[:-1]]), **kw)  # type: ignore[arg-type]


def rule(research: ResearchSettings, **change: object) -> NoiseBand:
    return NoiseBand(replace(load_noise(research), **change), research.zone)  # type: ignore[arg-type]


def prepared(research: ResearchSettings, days: list[Day], **change: object) -> NoiseBand:
    r = rule(research, **change)
    r.prepare(days)
    return r


def signals(
    r: NoiseBand, day: Day, state: DayState = FRESH, *, every: bool = False
) -> list[tuple[int, int]]:
    """(session minute of the signal bar, direction): the first signal of the day, as
    the engine asks with `state` (or, with `every`, every bar asked with `state`)."""
    out = []
    for i in range(len(day)):
        s = r.entry(day, i, state)
        if s is not None:
            minute = datetime.combine(day.day, day.clock[i]) - datetime.combine(
                day.day, time(9, 30)
            )
            out.append((minute // MINUTE, s.direction))
            if not every:
                break
    return out


DATES = weekdays(date(2024, 4, 1), 16)
HIST = history(DATES[:14])


# --- settings ---------------------------------------------------------------------------


def test_pre_registered_settings_are_read(research: ResearchSettings) -> None:
    assert load_noise(research) == NoiseSettings(
        rth_start=time(9, 30),
        rth_end=time(16, 0),
        min_rth_bars=300,
        warmup_days=14,
        band_multiple=1.5,
        first_check_minute=30,
        check_every_minutes=15,
        flat=time(15, 55),
        max_trades_per_session=1,
    )


def test_registered_without_variants(research: ResearchSettings) -> None:
    reg = RULES["noise_band"]
    assert reg.variants(research) == ()
    assert isinstance(reg.build(research, None), NoiseBand)
    with pytest.raises(ValueError, match="variant"):
        build_noise(research, "1.5")


def _copy(tmp_path: Path) -> Path:
    import shutil

    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    return tmp_path / "config"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('rth_end: "16:00"', 'rth_end: "09:30"', "rth_end"),
        ('rth_end: "16:00"', 'rth_end: "18:30"', "roll"),
        ('flat: "15:55"', 'flat: "16:00"', "flat"),
        ('flat: "15:55"', 'flat: "09:30"', "flat"),
        ("min_rth_bars: 300", "min_rth_bars: 0", "min_rth_bars"),
        ("warmup_days: 14", "warmup_days: 0", "warmup_days"),
        ("band_multiple: 1.5", "band_multiple: 0", "band_multiple"),
        ("band_multiple: 1.5", "band_multiple: .inf", "band_multiple"),
        ("first_check_minute: 30", "first_check_minute: -1", "first_check_minute"),
        ("check_every_minutes: 15", "check_every_minutes: 0", "check_every_minutes"),
        ("max_trades_per_session: 1 #", "max_trades_per_session: 0 #", "max_trades"),
        (
            "    max_trades_per_session: 1 #",
            "    extra: 1\n    max_trades_per_session: 1 #",
            "unknown",
        ),
    ],
)
def test_bad_rule_config_is_refused(tmp_path: Path, old: str, new: str, match: str) -> None:
    config = _copy(tmp_path)
    path = config / "research.yaml"
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))
    with pytest.raises(ConfigError, match=match):
        load_noise(load_research(config, "es.yaml"))


def test_missing_rule_section_is_refused(research: ResearchSettings) -> None:
    with pytest.raises(ConfigError, match="noise_band"):
        load_noise(replace(research, rules={}))


# --- sigma, range, bands ----------------------------------------------------------------


def test_sigma_range_and_bands_from_the_previous_kept_days(research: ResearchSettings) -> None:
    d = today(DATES[14], [100.0] * RTH_BARS)
    r = prepared(research, [*HIST, d])
    i = 44
    assert r.sigma(d, i) == pytest.approx(0.01)
    assert r.range_points(d) == pytest.approx(1.0)
    top, bottom = r.levels(d, i)  # type: ignore[misc]
    # open 100; previous kept close 99 (day 13 is odd): top from the open, bottom from 99
    assert top == pytest.approx(100 * (1 + 1.5 * 0.01))
    assert bottom == pytest.approx(99 * (1 - 1.5 * 0.01))


def test_top_uses_the_previous_close_when_it_is_above_the_open(
    research: ResearchSettings,
) -> None:
    d = today(DATES[14], [100.0] * RTH_BARS)
    up = history(DATES[:14])
    up[-1] = rth(DATES[13], [101.0] * RTH_BARS, opens=[100.0, *[101.0] * (RTH_BARS - 1)])
    r = prepared(research, [*up, d])
    top, bottom = r.levels(d, 44)  # type: ignore[misc]
    assert top == pytest.approx(101 * 1.015)  # the previous close 101 is above the open 100
    assert bottom == pytest.approx(100 * 0.985)


def test_sigma_takes_the_last_past_bar_at_or_before_the_minute(
    research: ResearchSettings,
) -> None:
    hist = history(DATES[:14])
    # day 0 (a = +1): a gap from minute 40 to 50; its bar at minute 39 closes at 105
    closes = [101.0] * RTH_BARS
    closes[39] = 105.0
    keep = [k for k in range(RTH_BARS) if not 40 <= k <= 50]
    stamps = [datetime.combine(DATES[0], time(9, 30), tzinfo=NY) + k * MINUTE for k in keep]
    hist[0] = _bars(
        DATES[0], stamps, [100.0] + [closes[k - 1] for k in keep[1:]], [closes[k] for k in keep]
    )
    d = today(DATES[14], [100.0] * RTH_BARS)
    r = prepared(research, [*hist, d])
    expected = statistics.fmean([0.05] + [0.01] * 13)
    assert r.sigma(d, 44) == pytest.approx(expected)  # minute 44 reads day 0's minute 39
    assert r.sigma(d, 39) == pytest.approx(expected)
    assert r.sigma(d, 38) == pytest.approx(0.01)
    assert r.sigma(d, 51) == pytest.approx(0.01)  # minute 51 is day 0's own bar again


def test_a_past_day_that_starts_later_is_left_out_of_the_mean(
    research: ResearchSettings,
) -> None:
    hist = history(DATES[:14])
    # day 0 starts at minute 50 with an open of 100 and closes at 110 (|move| 0.10)
    hist[0] = rth(
        DATES[0],
        [110.0] * (RTH_BARS - 50),
        opens=[100.0] + [110.0] * (RTH_BARS - 51),
        first_minute=50,
    )
    d = today(DATES[14], [100.0] * RTH_BARS)
    r = prepared(research, [*hist, d])
    assert r.sigma(d, 44) == pytest.approx(0.01)  # 13 days only
    assert r.sigma(d, 50) == pytest.approx(statistics.fmean([0.10] + [0.01] * 13))


def test_range_is_the_mean_rth_high_minus_low_of_the_previous_kept_days(
    research: ResearchSettings,
) -> None:
    hist = history(DATES[:14])
    highs = [max(o, c) for o, c in zip(hist[3].open, hist[3].close, strict=True)]
    highs[100] = 108.0
    hist[3] = replace(hist[3], high=tuple(highs))
    late = hist[5]  # a bar after the regular session does not count
    extra = make_day(datetime.combine(DATES[5], time(16, 0), tzinfo=NY), [150.0], day=DATES[5])
    hist[5] = _join(late, extra)
    d = today(DATES[14], [100.0] * RTH_BARS)
    r = prepared(research, [*hist, d])
    assert r.range_points(d) == pytest.approx(statistics.fmean([1.0] * 13 + [9.0]))


# --- checkpoints, first signal only, strictness ----------------------------------------


def test_checked_only_at_the_pre_registered_minutes(research: ResearchSettings) -> None:
    d = today(DATES[14], [120.0] * RTH_BARS)  # far above the band all day
    r = prepared(research, [*HIST, d], max_trades_per_session=99)
    minutes = [m for m, _ in signals(r, d, every=True)]
    assert minutes == list(range(44, 375, 15))  # 44, 59, ..., 374; 389 is at/after 15:55
    r = prepared(research, [*HIST, d], max_trades_per_session=99, first_check_minute=45)
    assert signals(r, d, every=True)[0][0] == 59
    r = prepared(research, [*HIST, d], max_trades_per_session=99, check_every_minutes=30)
    assert [m for m, _ in signals(r, d, every=True)][:2] == [59, 89]


def test_first_check_minute_is_inclusive(research: ResearchSettings) -> None:
    d = today(DATES[14], [120.0] * RTH_BARS)
    r = prepared(research, [*HIST, d], first_check_minute=44)
    assert signals(r, d) == [(44, 1)]
    r = prepared(research, [*HIST, d], first_check_minute=45)
    assert signals(r, d) == [(59, 1)]


def test_no_signal_from_the_flat_time_on(research: ResearchSettings) -> None:
    d = today(DATES[14], [120.0] * RTH_BARS)
    r = prepared(research, [*HIST, d], max_trades_per_session=99, flat=time(14, 59))
    assert [m for m, _ in signals(r, d, every=True)][-1] == 314  # 329 is the 14:59 bar itself
    r = prepared(research, [*HIST, d], max_trades_per_session=99, flat=time(15, 0))
    # 329 (14:59) is before the flat time, but its next bar opens AT it (NB-11)
    assert [m for m, _ in signals(r, d, every=True)][-1] == 314
    r = prepared(research, [*HIST, d], max_trades_per_session=99, flat=time(15, 1))
    assert [m for m, _ in signals(r, d, every=True)][-1] == 329


def test_first_signal_of_the_day_only(research: ResearchSettings) -> None:
    d = today(DATES[14], [120.0] * RTH_BARS)
    r = prepared(research, [*HIST, d])
    assert signals(r, d) == [(44, 1)]
    assert signals(r, d, DayState(1, -500.0), every=True) == []  # one trade taken: no more
    r = prepared(research, [*HIST, d], max_trades_per_session=2)
    assert signals(r, d, DayState(1, -500.0)) == [(44, 1)]
    assert signals(r, d, DayState(2, -500.0), every=True) == []


def test_call_above_put_below_and_the_band_itself_is_nothing(
    research: ResearchSettings,
) -> None:
    flat = today(DATES[14], [100.0] * RTH_BARS)
    top, bottom = prepared(research, [*HIST, flat]).levels(flat, 44)  # type: ignore[misc]
    assert top == pytest.approx(101.5)
    assert bottom == pytest.approx(99 * 0.985)
    for close, expected in (
        (top, []),
        (top + 0.01, [(44, 1)]),
        (bottom, []),
        (bottom - 0.01, [(44, -1)]),
        (100.0, []),
    ):
        closes = [100.0] * RTH_BARS
        closes[44] = close
        d = today(DATES[14], closes)
        r = prepared(research, [*HIST, d])
        assert signals(r, d) == expected, close


def test_put_signal_carries_the_range_and_next_open(research: ResearchSettings) -> None:
    closes = [100.0] * RTH_BARS
    closes[59] = 95.0
    d = today(DATES[14], closes)
    s = prepared(research, [*HIST, d]).entry(d, 59, FRESH)
    assert s is not None
    assert (s.direction, s.next_open) == (-1, True)
    assert s.range_points == pytest.approx(1.0)


def test_signal_reads_the_trigger_close_not_its_high(research: ResearchSettings) -> None:
    closes = [100.0] * RTH_BARS
    highs = [100.0] * RTH_BARS
    highs[44] = 130.0
    d = today(DATES[14], closes, highs=highs)
    r = prepared(research, [*HIST, d])
    assert signals(r, d) == []


def test_signal_carries_the_range_and_enters_at_the_next_open(
    research: ResearchSettings,
) -> None:
    closes = [100.0] * RTH_BARS
    closes[59] = 103.0
    d = today(DATES[14], closes)
    r = prepared(research, [*HIST, d])
    s = r.entry(d, 59, FRESH)
    assert s is not None
    assert s.direction == 1
    assert s.next_open
    assert s.range_points == pytest.approx(1.0)
    assert s.futures_stop is None
    assert s.flat_by == datetime.combine(DATES[14], time(15, 55), tzinfo=NY)


def test_a_kept_day_ending_before_the_flat_time_is_not_traded(
    research: ResearchSettings,
) -> None:
    """NB-12: the source would trade it and exit at its last regular close; the harness
    would flat at a later bar (after the session). Fail closed: not traded, reported."""
    closes = [120.0] * 360  # minutes 0..359 (last bar 15:29), then a 16:00 bar
    d = _join(
        today(DATES[14], closes),
        make_day(datetime.combine(DATES[14], time(16, 0), tzinfo=NY), [120.0], day=DATES[14]),
    )
    r = prepared(research, [*HIST, d], max_trades_per_session=99)
    assert r.kept(d)
    assert signals(r, d, every=True) == []
    assert any(line.startswith("NB-12") and str(DATES[14]) in line for line in r.report([*HIST, d]))
    # a last regular bar opening one minute before the flat time is still early; one
    # opening AT the flat time is not
    early = today(DATES[14], [120.0] * 385)  # minutes 0..384
    r = prepared(research, [*HIST, early])
    assert signals(r, early) == []
    full = today(DATES[14], [120.0] * 386)  # minutes 0..385
    r = prepared(research, [*HIST, full])
    assert signals(r, full) == [(44, 1)]


def test_no_lookahead_into_later_prices(research: ResearchSettings) -> None:
    closes = [100.0] * RTH_BARS
    closes[59] = 103.0
    d = today(DATES[14], closes)
    later = list(closes)
    for k in range(60, RTH_BARS):
        later[k] = 50.0
    d2 = today(DATES[14], later)
    r1, r2 = prepared(research, [*HIST, d]), prepared(research, [*HIST, d2])
    for i in range(60):
        assert r1.entry(d, i, FRESH) == r2.entry(d2, i, FRESH)


# --- keep and warm-up rules ------------------------------------------------------------


def test_warm_up_needs_the_full_number_of_kept_days(research: ResearchSettings) -> None:
    d = today(DATES[14], [120.0] * RTH_BARS)
    r = prepared(research, [*HIST[1:], d])
    assert signals(r, d) == []
    assert r.sigma(d, 44) is None
    assert r.range_points(d) is None
    r = prepared(research, [*HIST, d], warmup_days=13)
    assert signals(r, d) == [(44, 1)]


def test_short_days_are_not_kept_traded_or_counted(research: ResearchSettings) -> None:
    short = today(DATES[14], [120.0] * 299)  # one bar below the minimum
    d = today(DATES[15], [120.0] * RTH_BARS)
    r = prepared(research, [*HIST, short, d])
    assert signals(r, short) == []
    assert signals(r, d) == [(44, 1)]
    # the short day is not the previous close and not in the history
    assert r.levels(d, 44)[1] == pytest.approx(99 * 0.985)  # type: ignore[index]
    ok = rth(DATES[14], [120.0] * 300, opens=[100.0, *[120.0] * 299], first_minute=90)
    r = prepared(research, [*HIST, ok, d])
    assert signals(r, ok) == [(104, 1)]  # 11:00-15:59: kept with exactly the minimum


def test_bars_outside_the_regular_session_do_not_count(research: ResearchSettings) -> None:
    pre = make_day(
        datetime.combine(DATES[14], time(8, 0), tzinfo=NY), [130.0] * 90, day=DATES[14]
    )  # 08:00-09:29
    body = today(DATES[14], [100.0] * 299)
    d = _join(pre, body)
    r = prepared(research, [*HIST, d])
    assert not r.kept(d)  # 389 bars in the day, 299 of them regular
    full = _join(pre, today(DATES[14], [100.0] * RTH_BARS))
    r = prepared(research, [*HIST, full])
    assert r.kept(full)
    assert r.levels(full, 90 + 44)[0] == pytest.approx(100 * 1.015)  # type: ignore[index]


def test_weekend_days_are_not_kept(research: ResearchSettings) -> None:
    saturday = date(2024, 4, 20)
    sat = rth(saturday, [500.0] * RTH_BARS, opens=[100.0, *[500.0] * (RTH_BARS - 1)])
    hist = history(weekdays(date(2024, 4, 1), 14))
    d = today(date(2024, 4, 22), [100.0] * RTH_BARS)
    r = prepared(research, [*hist, sat, d])
    assert not r.kept(sat)
    assert r.levels(d, 44)[1] == pytest.approx(99 * 0.985)  # type: ignore[index]


def test_entry_on_an_unprepared_day_is_an_error(research: ResearchSettings) -> None:
    r = rule(research)
    with pytest.raises(ValueError, match="prepare"):
        r.entry(HIST[0], 44, FRESH)


def test_report_counts_the_days(research: ResearchSettings) -> None:
    short = today(DATES[14], [100.0] * 299)
    d = today(DATES[15], [100.0] * RTH_BARS)
    r = prepared(research, [*HIST, short, d])
    lines = r.report([*HIST, short, d])
    text = "\n".join(lines)
    assert lines[0] == (
        "session days: 16; kept: 15; not kept (not a weekday, or too few regular bars): 1"
    )
    assert "warming up: 14; tradable: 1" in text
    assert "full day's bar count" in text


# --- through the engine: next-bar-open entry, flat, DST ---------------------------------


def _plain(research: ResearchSettings) -> ResearchSettings:
    return replace(research, option=replace(research.option, reference_price=None))


def test_engine_enters_at_the_next_bar_open_and_flattens_at_15_55(
    research: ResearchSettings,
) -> None:
    s = _plain(research)
    closes = [100.0] * RTH_BARS
    closes[44] = 103.0
    opens = [100.0, *closes[:-1]]
    opens[45] = 103.5  # the next bar opens away from the trigger close
    d = today(DATES[14], closes, opens=opens)
    r = build_noise(s, None)
    trades, skipped = run([*HIST, d], r, VOL, s, "booker")
    assert skipped == 0
    (t,) = trades
    assert t.entry_ts == datetime.combine(DATES[14], time(10, 15), tzinfo=NY)
    k = round(103.5 / s.option.strike_step) * s.option.strike_step
    expected = price(
        Right76.CALL,
        forward=103.5,
        strike=k,
        years=s.option.days_to_expiry / s.option.days_per_year,
        vol=0.2,
        rate=0.0,
    )
    assert t.entry_value == pytest.approx(expected)
    assert t.exit_ts == datetime.combine(DATES[14], time(15, 55), tzinfo=NY)
    assert t.reasons == ("flat",)


def test_dst_days_use_new_york_wall_time(research: ResearchSettings) -> None:
    """Across the March clock change the regular session moves an hour in UTC; the
    session minutes (and so the checkpoints) follow New York wall time."""
    dates = weekdays(date(2024, 2, 26), 16)  # DST starts 2024-03-10
    hist = history(dates[:14])
    closes = [100.0] * RTH_BARS
    closes[44] = 103.0
    d = today(dates[14], closes)
    r = prepared(research, [*hist, d])
    assert signals(r, d) == [(44, 1)]
    s = _plain(research)
    (t,) = run([*hist, d], build_noise(s, None), VOL, s, "booker")[0]
    assert t.entry_ts.astimezone(UTC) == datetime(2024, 3, 15, 14, 15, tzinfo=UTC)
    before = weekdays(date(2024, 2, 5), 15)
    d0 = today(before[14], closes)
    r = prepared(research, [*history(before[:14]), d0])
    assert signals(r, d0) == [(44, 1)]
    assert d0.ts[45] == datetime(2024, 2, 23, 15, 15, tzinfo=UTC)


# --- parity with the source rule on synthetic data -------------------------------------


def _synthetic_csv(path: Path, seed: int) -> None:
    """About eight weeks of 1-minute bars, February to April 2024 (the March clock change
    inside): overnight bars, regular-session random walks (some days trending), a half
    day, a day with a gap, a Saturday session and post-close bars."""
    rng = random.Random(seed)
    rows: list[str] = []
    p = 5000.0
    d = date(2024, 2, 12)
    while d <= date(2024, 4, 12):
        weekday = d.weekday()
        if weekday == 6:
            d += timedelta(days=1)
            continue
        drift = rng.choice([0.0, 0.0, 0.0, rng.uniform(-0.6, 0.6)])
        minutes: list[int] = list(range(-60, 0))  # 08:30-09:29
        n_rth = RTH_BARS
        if rng.random() < 0.08:
            n_rth = rng.choice([210, 299, 300, 330])  # half days and near the minimum
        body = list(range(n_rth))
        if rng.random() < 0.1:
            cut = rng.randrange(40, 300)
            body = [m for m in body if not cut <= m < cut + rng.randrange(1, 40)]
        if rng.random() < 0.1:
            body = body[rng.randrange(1, 20) :]  # a late start
        minutes += body
        minutes += list(range(RTH_BARS, RTH_BARS + 30))  # 16:00-16:29
        if weekday == 5 and rng.random() < 0.5:
            minutes = list(range(RTH_BARS))
        elif weekday == 5:
            d += timedelta(days=1)
            continue
        for m in minutes:
            ts = datetime.combine(d, time(9, 30), tzinfo=NY) + m * MINUTE
            o = p
            step = drift + rng.gauss(0.0, 1.5)
            c = max(o + step, 1.0)
            hi = max(o, c) + abs(rng.gauss(0.0, 0.5))
            lo = min(o, c) - abs(rng.gauss(0.0, 0.5))
            o, hi, lo, c = (round(x * 4) / 4 for x in (o, hi, lo, c))
            hi, lo = max(hi, o, c), min(lo, o, c)
            rows.append(f"{ts.astimezone(UTC).isoformat()},{o:.2f},{hi:.2f},{lo:.2f},{c:.2f},7")
            p = c
        p += rng.gauss(0.0, 5.0)  # overnight gap
        d += timedelta(days=1)
    path.write_text("ts,open,high,low,close,volume\n" + "\n".join(rows) + "\n")


def _harness_first_signals(research: ResearchSettings, path: Path) -> list[tuple[object, int, int]]:
    days = load_days(path, "ES", research.zone, research.roll)
    r = build_noise(research, None)
    r.prepare(days)
    out: list[tuple[object, int, int]] = []
    for day in days:
        for i in range(len(day)):
            s = r.entry(day, i, FRESH)
            if s is not None:
                clock = day.clock[i + 1]
                minute = datetime.combine(day.day, clock) - datetime.combine(day.day, time(9, 30))
                out.append((day.day, s.direction, minute // MINUTE))
                break
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_signals_match_the_source_rule(
    research: ResearchSettings, tmp_path: Path, seed: int
) -> None:
    path = tmp_path / "bars.csv"
    _synthetic_csv(path, seed)
    with path.open(newline="") as handle:
        source = ref.load(csv.DictReader(handle))
    # the harness's two fail-closed refusals (NB-11, NB-12), not in the source rule
    early = {d.date for d in source if d.m[-1] < ref.FLAT_MINUTE}
    expected = [
        (d, x, m) for d, x, m in ref.first_signals(source) if d not in early and m < ref.FLAT_MINUTE
    ]
    got = _harness_first_signals(research, path)
    assert len(expected) >= 5  # the data makes signals
    assert {direction for _, direction, _ in expected} == {1, -1}
    assert got == expected


# --- helpers ----------------------------------------------------------------------------


def _bars(d: date, stamps: list[datetime], opens: list[float], closes: list[float]) -> Day:
    utc = tuple(s.astimezone(UTC) for s in stamps)
    return Day(
        day=d,
        ts=utc,
        clock=tuple(s.astimezone(NY).time() for s in stamps),
        open=tuple(opens),
        high=tuple(max(o, c) for o, c in zip(opens, closes, strict=True)),
        low=tuple(min(o, c) for o, c in zip(opens, closes, strict=True)),
        close=tuple(closes),
        volume=tuple(1 for _ in stamps),
    )


def _join(a: Day, b: Day) -> Day:
    return Day(
        day=a.day,
        ts=a.ts + b.ts,
        clock=a.clock + b.clock,
        open=a.open + b.open,
        high=a.high + b.high,
        low=a.low + b.low,
        close=a.close + b.close,
        volume=a.volume + b.volume,
    )
