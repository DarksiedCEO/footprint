from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from footprint.core.config import ConfigError, load_instrument, load_risk


def rewrite(source: Path, tmp_path: Path, **changes: Any) -> Path:
    """Copy a real profile with some keys changed; a value of ... drops the key."""
    data = yaml.safe_load(source.read_text())
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    target = tmp_path / source.name
    target.write_text(yaml.safe_dump(data))
    return target


def session(**changes: str) -> dict[str, str]:
    return {"start": "04:00", "end": "14:00", "timezone": "America/Los_Angeles"} | changes


def test_instrument_profile_loads(instrument_path: Path) -> None:
    profile = load_instrument(instrument_path)
    assert profile.symbol == "NQ"
    assert profile.exchange == "CME"
    assert profile.point_value == Decimal("20")
    assert profile.tick_size == Decimal("0.25")
    assert profile.round_number_step == Decimal("100")
    assert (profile.prime_session.start, profile.prime_session.end) == (time(4, 0), time(14, 0))
    assert profile.prime_session.timezone == "America/Los_Angeles"
    assert (profile.overnight_session.start, profile.overnight_session.end) == (
        time(15, 0),
        time(3, 59),
    )
    assert profile.overnight_session.timezone == "America/Los_Angeles"
    assert (profile.dte_min, profile.dte_max) == (1, 3)
    assert (profile.cash_session.start, profile.cash_session.end) == (time(6, 30), time(13, 0))
    assert profile.option_tick_size == Decimal("0.25")


def test_risk_profile_loads(risk_path: Path) -> None:
    profile = load_risk(risk_path)
    assert profile.bankroll == Decimal("25000")
    assert profile.position_cap == Decimal("5000")
    assert profile.overnight_position_cap == Decimal("2500")
    assert profile.profit_target == Decimal("1000")
    assert (profile.stop_min, profile.stop_max) == (Decimal("750"), Decimal("1000"))
    assert profile.daily_max_stopouts == 2
    assert profile.daily_max_loss == Decimal("2000")
    assert profile.weekly_max_loss == Decimal("3500")
    assert profile.kill_switch_drawdown_pct == Decimal("20")
    assert (profile.max_trades_prime, profile.max_trades_overnight) == (4, 0)
    assert profile.stop_loss == Decimal("750")
    assert profile.trail_keep_pct == Decimal("50")
    assert profile.max_contracts == 1
    assert profile.fee_per_contract_per_side == Decimal("2.5")


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": ""},
        {"symbol": 5},
        {"exchange": ...},
        {"point_value": 0},
        {"point_value": -20},
        {"point_value": "20"},
        {"point_value": True},
        {"tick_size": 0},
        {"tick_size": float("nan")},
        {"round_number_step": 0.3},  # not a multiple of tick_size
        {"dte_min": -1},
        {"dte_min": 1.5},
        {"dte_min": 5},  # above dte_max
        {"prime_session": "04:00-14:00"},
        {"prime_session": session(start="25:00")},
        {"prime_session": session(start="4am")},
        {"prime_session": session(end="04:00")},  # zero-length
        {"prime_session": session(timezone="Mars/Olympus")},
        {"prime_session": {"start": "04:00", "end": "14:00"}},
        {"overnight_session": ...},
        {"cash_session": ...},
        {"cash_session": session(start="03:00", end="13:00")},  # starts before prime
        {"cash_session": session(start="06:30", end="15:00")},  # ends after prime
        {"cash_session": session(start="13:00", end="06:30")},  # backwards
        {"cash_session": session(start="06:30", end="13:00", timezone="America/New_York")},
        {"option_tick_size": 0},
        {"option_tick_size": ...},
        {"unexpected": 1},
    ],
)
def test_instrument_rejects_bad_values(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError):
        load_instrument(rewrite(instrument_path, tmp_path, **changes))


def overnight(**changes: str) -> dict[str, str]:
    return session(start="15:00", end="03:59") | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"overnight_session": overnight(end="04:00")},  # shares prime's first minute
        {"overnight_session": overnight(start="14:00")},  # shares prime's last minute
        {"overnight_session": overnight(start="13:00")},
        {"overnight_session": overnight(end="09:00")},
        {"overnight_session": overnight(start="05:00", end="06:00")},  # inside prime
        {"overnight_session": overnight(start="03:00", end="15:00")},  # contains prime
        {"prime_session": session(start="16:00", end="17:00")},  # inside overnight
        {"prime_session": session(start="02:00", end="03:00")},  # inside the wrapped part
        {"prime_session": session(start="20:00", end="10:00")},  # both wrap midnight
    ],
)
def test_instrument_rejects_overlapping_sessions(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any]
) -> None:
    # A prime window that wraps midnight is rejected even earlier, as crossing midnight.
    with pytest.raises(ConfigError, match=r"must not overlap|must not cross midnight"):
        load_instrument(rewrite(instrument_path, tmp_path, **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"overnight_session": overnight(start="14:01")},  # adjacent to prime's end
        {"overnight_session": overnight(start="22:00", end="23:00")},
        {"prime_session": session(start="06:00", end="14:00")},
    ],
)
def test_instrument_accepts_adjacent_or_separate_sessions(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any]
) -> None:
    load_instrument(rewrite(instrument_path, tmp_path, **changes))


def test_instrument_rejects_sessions_in_different_timezones(
    instrument_path: Path, tmp_path: Path
) -> None:
    bad = rewrite(
        instrument_path, tmp_path, overnight_session=overnight(timezone="America/New_York")
    )
    with pytest.raises(ConfigError, match="same timezone"):
        load_instrument(bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"bankroll": 0},
        {"bankroll": -25000},
        {"bankroll": "lots"},
        {"bankroll": ...},
        {"position_cap": 30000},  # above bankroll
        {"overnight_position_cap": 6000},  # above position_cap
        {"profit_target": 0},
        {"stop_min": 1500},  # above stop_max
        {"stop_max": -1},
        {"daily_max_stopouts": 0},
        {"daily_max_stopouts": 2.5},
        {"daily_max_loss": 4000},  # above weekly_max_loss
        {"weekly_max_loss": None},
        {"kill_switch_drawdown_pct": 0},
        {"kill_switch_drawdown_pct": 150},
        {"max_trades_prime": -1},
        {"max_trades_overnight": True},
        {"leverage": 10},
    ],
)
def test_risk_rejects_bad_values(risk_path: Path, tmp_path: Path, changes: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_risk(rewrite(risk_path, tmp_path, **changes))


def test_error_names_the_file_and_key(risk_path: Path, tmp_path: Path) -> None:
    bad = rewrite(risk_path, tmp_path, bankroll=-1)
    with pytest.raises(ConfigError, match=rf"{bad.name}: bankroll"):
        load_risk(bad)


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "symbol: [unclosed"])
def test_rejects_malformed_files(tmp_path: Path, content: str) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(content)
    with pytest.raises(ConfigError):
        load_instrument(bad)
    with pytest.raises(ConfigError):
        load_risk(bad)


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_risk(tmp_path / "nope.yaml")


@pytest.mark.parametrize("bad_time", ["03:59:30", "03:59:00.5"])
def test_session_times_with_seconds_are_rejected(
    instrument_path: Path, tmp_path: Path, bad_time: str
) -> None:
    data = yaml.safe_load(instrument_path.read_text())
    data["overnight_session"]["end"] = bad_time
    target = tmp_path / "nq.yaml"
    target.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="no seconds"):
        load_instrument(target)


def test_trading_days_and_holidays_load(instrument_path: Path) -> None:
    profile = load_instrument(instrument_path)
    assert profile.trading_days == frozenset({0, 1, 2, 3, 4})  # Mon-Fri
    assert date(2026, 12, 25) in profile.holidays
    assert profile.early_closes[date(2026, 11, 26)] == time(10, 0)
    assert profile.calendar_through == date(2027, 3, 31)
    assert profile.calendar_warn_days == 30


@pytest.mark.parametrize(
    "changes",
    [
        {"trading_days": []},
        {"trading_days": ["MON", "MON"]},
        {"trading_days": ["Monday"]},
        {"trading_days": "MON"},
        {"holidays": ["2026-12-25"]},
        {"holidays": "2026-12-25"},
        {"trading_days": ...},
        {"holidays": ...},
    ],
)
def test_bad_trading_calendar_is_rejected(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError):
        load_instrument(rewrite(instrument_path, tmp_path, **changes))


def test_holiday_dates_are_parsed(instrument_path: Path, tmp_path: Path) -> None:
    listed = yaml.safe_load(instrument_path.read_text())["holidays"]
    target = rewrite(instrument_path, tmp_path, holidays=list(reversed(listed)))
    assert load_instrument(target).holidays == frozenset(listed)
    assert date(2026, 12, 25) in load_instrument(target).holidays


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({"calendar_through": date(2099, 12, 31)}, "easter_sundays has no date in 2028"),
        ({"calendar_through": date(2027, 5, 31)}, "2027-05-31 (Memorial Day)"),
        ({"holidays": ["2026-04-03", "2026-12-25", "2027-01-01", "2027-03-26"]}, "New Year"),
        # AEGIS N3: a closure listed as an early close is not a closure
        (
            {
                "holidays": ["2026-01-01", "2026-04-03", "2027-01-01", "2027-03-26"],
                "early_closes": "christmas_as_early",
            },
            "2026-12-25 (Christmas Day)",
        ),
        ({"early_closes": "drop 2026-11-27"}, "Day after Thanksgiving, early close"),
        ({"early_closes": "drop 2026-12-24"}, "Christmas Eve, early close"),
    ],
)
def test_an_incomplete_calendar_is_refused(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any], missing: str
) -> None:
    data = dict(changes)
    if "holidays" in data:
        data["holidays"] = [date.fromisoformat(d) for d in data["holidays"]]
    early = data.get("early_closes")
    if isinstance(early, str):
        listed = dict(yaml.safe_load(instrument_path.read_text())["early_closes"])
        if early == "christmas_as_early":
            listed[date(2026, 12, 25)] = "10:00"
        else:
            listed.pop(date.fromisoformat(early.split()[1]))
        data["early_closes"] = listed
    target = rewrite(instrument_path, tmp_path, **data)
    with pytest.raises(ConfigError) as refused:  # AEGIS re-audit M-3
        load_instrument(target)
    assert missing in str(refused.value)


def test_an_early_close_rule_is_satisfied_by_a_full_closure(
    instrument_path: Path, tmp_path: Path
) -> None:
    """H13: when an early-close day is closed outright (Christmas Eve as a holiday),
    the calendar is still complete."""
    data = yaml.safe_load(instrument_path.read_text())
    early = dict(data["early_closes"])
    early.pop(date(2026, 12, 24))
    target = rewrite(
        instrument_path,
        tmp_path,
        early_closes=early,
        holidays=[*data["holidays"], date(2026, 12, 24)],
    )
    assert date(2026, 12, 24) in load_instrument(target).holidays


def test_days_after_applies_to_a_fixed_date_rule() -> None:
    """F6: days_after is not silently ignored on a month-and-day rule."""
    from footprint.core.holiday_rules import HolidayRule, rule_date

    rule = HolidayRule("Day after Christmas", month=12, day=25, days_after=1)
    assert rule_date(rule, 2026, {}) == date(2026, 12, 26)


def test_the_holiday_rules_in_nq_yaml(instrument_path: Path) -> None:
    from footprint.core.holiday_rules import missing_days, rule_date

    nq = load_instrument(instrument_path)
    easters = {d.year: d for d in nq.easter_sundays}
    days = {r.name: rule_date(r, 2026, easters) for r in nq.holiday_rules}
    assert days == {
        "New Year's Day": date(2026, 1, 1),
        "Martin Luther King Jr. Day": date(2026, 1, 19),
        "Presidents Day": date(2026, 2, 16),
        "Good Friday": date(2026, 4, 3),
        "Memorial Day": date(2026, 5, 25),
        "Juneteenth": date(2026, 6, 19),
        "Independence Day": date(2026, 7, 3),  # Saturday -> Friday
        "Labor Day": date(2026, 9, 7),
        "Thanksgiving": date(2026, 11, 26),
        "Day after Thanksgiving": date(2026, 11, 27),
        "Christmas Eve": date(2026, 12, 24),
        "Christmas Day": date(2026, 12, 25),
    }
    rules = {r.name: r for r in nq.holiday_rules}
    assert rule_date(rules["New Year's Day"], 2022, {}) is None  # a Saturday: not observed
    assert rule_date(rules["New Year's Day"], 2023, {}) == date(2023, 1, 2)  # Sunday -> Mon
    assert rule_date(rules["Christmas Day"], 2027, {}) == date(2027, 12, 24)
    assert rule_date(rules["Memorial Day"], 2027, {}) == date(2027, 5, 31)
    assert rule_date(rules["Day after Thanksgiving"], 2026, {}) == date(2026, 11, 27)
    assert rule_date(rules["Christmas Eve"], 2026, {}) == date(2026, 12, 24)
    assert rule_date(rules["Christmas Eve"], 2027, {}) == date(2027, 12, 24)
    assert rule_date(rules["Christmas Eve"], 2028, {}) is None  # a Sunday
    closed = {*nq.holidays, *nq.no_trade_days}
    early = set(nq.early_closes)
    assert (
        missing_days(
            nq.holiday_rules,
            nq.easter_sundays,
            closed,
            early,
            date(2026, 1, 1),
            nq.calendar_through,
        )
        == []
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"easter_sundays": [date(2026, 4, 5)]}, "no date in 2027"),
        ({"easter_sundays": [date(2026, 4, 5), date(2027, 3, 27)]}, "not a Sunday"),
        ({"holiday_rules": [{"name": "X", "month": 13, "day": 1}]}, "holiday_rules"),
        ({"holiday_rules": [{"name": "X", "month": 2, "weekday": "MON", "nth": 6}]}, "nth"),
        ({"holiday_rules": [{"name": "X", "month": 2}]}, "exactly one"),
        ({"holiday_rules": [{"name": "X", "month": 2, "day": 1, "observed": "x"}]}, "observed"),
        ({"holiday_rules": [{"name": "X", "month": 2, "day": 1, "extra": 1}]}, "only"),
        ({"holiday_rules": []}, "non-empty"),
        (  # AEGIS LOW-4: deleting a rule (and its day) does not switch its check off
            {"holiday_rules": [{"name": "Christmas Day", "month": 12, "day": 25}]},
            "no rule for New Year",
        ),
        ({"holiday_rules": "drop Good Friday"}, "no rule for Good Friday"),  # G9/G10: one
        ({"holiday_rules": "drop Christmas Eve"}, "no rule for Christmas Eve"),
        ({"holiday_rules": "early Christmas Day"}, "Christmas Day must be closed"),  # F3
        ({"holiday_rules": "twice Christmas Day"}, "more than one rule named"),  # I-3
        ({"holiday_rules": [{"name": "X", "month": 2, "day": 1, "kind": "half"}]}, "kind"),
    ],
)
def test_bad_holiday_rules_are_refused(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    rules = changes.get("holiday_rules")
    if isinstance(rules, str):  # "drop <name>" / "early <name>": the real rules, edited
        verb, _, name = rules.partition(" ")
        current = yaml.safe_load(instrument_path.read_text())["holiday_rules"]
        if verb == "drop":
            changes = {"holiday_rules": [r for r in current if r["name"] != name]}
            assert len(changes["holiday_rules"]) == len(current) - 1
        elif verb == "twice":
            twin = {"name": name, "month": 1, "day": 1}
            changes = {"holiday_rules": [*current, twin]}
        else:
            changes = {
                "holiday_rules": [
                    {**r, "kind": "early_close"} if r["name"] == name else r for r in current
                ]
            }
    target = rewrite(instrument_path, tmp_path, **changes)
    with pytest.raises(ConfigError, match=message):
        load_instrument(target)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"early_closes": {date(2026, 12, 25): "10:00"}}, "both a holiday and an early close"),
        ({"early_closes": {date(2026, 11, 27): "14:30"}}, "before the normal prime close"),
        ({"early_closes": {date(2026, 11, 27): "06:00"}}, "after the cash open"),
        ({"early_closes": {date(2026, 11, 27): "10:00:30"}}, "HH:MM"),
        ({"early_closes": {date(2026, 11, 27): "ten"}}, "HH:MM"),
        ({"early_closes": {"2026-11-27": "10:00"}}, "keys must be dates"),
        ({"early_closes": ["2026-11-27"]}, "mapping"),
        ({"calendar_through": date(2026, 12, 1)}, "after calendar_through"),
        ({"calendar_through": "2027-03-31"}, "must be a date"),
        ({"calendar_warn_days": 0}, "calendar_warn_days"),
        ({"early_closes": ...}, "early_closes"),
        ({"calendar_through": ...}, "calendar_through"),
    ],
)
def test_bad_early_closes_and_calendar_ends_are_rejected(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_instrument(rewrite(instrument_path, tmp_path, **changes))


def test_prime_session_crossing_midnight_is_rejected(instrument_path: Path, tmp_path: Path) -> None:
    data = yaml.safe_load(instrument_path.read_text())
    data["prime_session"] = session(start="22:00", end="02:00")
    data["overnight_session"] = session(start="03:00", end="20:00")
    target = tmp_path / "nq.yaml"
    target.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="cross midnight"):
        load_instrument(target)


def test_strategy_profile_loads(strategy_path: Path) -> None:
    from footprint.core.config import load_strategy

    s = load_strategy(strategy_path)
    assert (s.trend.timeframe_minutes, s.trend.ema_fast, s.trend.ema_slow) == (15, 9, 21)
    assert s.trend.swing_strength == 2
    assert (s.volume.timeframe_minutes, s.volume.lookback_sessions) == (1, 20)
    assert s.volume.spike_multiple == Decimal("3")
    assert s.range.adr_days == 10
    assert (s.range.quiet_below_pct, s.range.extended_above_pct) == (Decimal(70), Decimal(100))
    assert s.range.chase_zone_pct == Decimal(10)
    assert (s.levels.room_multiple, s.levels.wall_multiple) == (Decimal("1.5"), Decimal(1))
    assert s.levels.swing_lookback_bars == 96
    assert s.data.max_bar_age_seconds == 120
    assert s.data.min_session_coverage_pct == Decimal(90)
    assert s.data.max_gap_minutes == 5
    assert s.options.assumed_delta == Decimal("0.45")
    assert (s.zeroday.lookback_sessions, s.zeroday.trip_multiple) == (20, Decimal(4))
    assert s.zeroday.volume_trip_multiple == Decimal(10)
    assert s.zeroday.recent_bars == 5
    assert s.zeroday.spike_bar_trip_multiple == Decimal(8)
    assert (s.console.levels_weight, s.console.range_weight) == (2, 1)
    assert (s.console.fire_threshold, s.console.close_lead_minutes) == (3, 5)
    assert s.theta.planned_hold_minutes == 60
    assert s.theta.max_decay_pct_of_target == Decimal(25)
    assert s.theta.rate == Decimal("0.04")
    assert (s.theta.min_implied_vol, s.theta.max_implied_vol) == (Decimal("0.05"), Decimal("1.5"))
    assert (s.vega.min_implied_vol, s.vega.max_implied_vol) == (Decimal("0.08"), Decimal("1.0"))
    assert (s.delta.min_abs_delta, s.delta.max_abs_delta) == (Decimal("0.45"), Decimal("0.7"))
    assert s.gamma.min_hours_to_expiry == Decimal(6)


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("trend", {"ema_fast": 21}),
        ("trend", {"ema_slow": 0}),
        ("trend", {"extra": 1}),
        ("volume", {"spike_multiple": 1}),
        ("volume", {"lookback_sessions": 0}),
        ("range", {"quiet_below_pct": 100}),
        ("range", {"adr_days": True}),
        ("range", {"chase_zone_pct": 100}),
        ("levels", {"wall_multiple": 2}),
        ("levels", {"swing_lookback_bars": 0}),
        ("levels", {"swing_lookback_bars": 4}),  # < 2 * swing_strength + 1
        ("data", {"max_bar_age_seconds": 0}),
        ("data", {"min_session_coverage_pct": 101}),
        ("data", {"max_gap_minutes": -1}),
        ("options", {"assumed_delta": 1}),
        ("options", {"assumed_delta": 0}),
        ("zeroday", {"trip_multiple": 1}),
        ("zeroday", {"volume_trip_multiple": 0.5}),
        ("zeroday", {"lookback_sessions": 0}),
        ("zeroday", {"recent_bars": 1}),  # must cover the two-bar trigger
        ("zeroday", {"spike_bar_trip_multiple": 3}),  # below trip_multiple
        ("console", {"fire_threshold": 4}),  # above the maximum score
        ("console", {"fire_threshold": 2}),  # levels alone could fire
        ("console", {"levels_weight": 0}),
        ("console", {"close_lead_minutes": -1}),
        ("theta", {"planned_hold_minutes": 0}),
        ("theta", {"planned_hold_minutes": 1.5}),
        ("theta", {"max_decay_pct_of_target": 0}),
        ("theta", {"max_decay_pct_of_target": 101}),
        ("theta", {"rate": "0.04"}),
        ("theta", {"rate": True}),
        ("theta", {"rate": float("inf")}),
        ("theta", {"rate": float("nan")}),
        ("theta", {"min_implied_vol": 0}),
        ("theta", {"max_implied_vol": 0.05}),  # == min_implied_vol
        ("theta", {"max_implied_vol": 0.01}),  # below min_implied_vol
        ("theta", {"extra": 1}),
        ("vega", {"min_implied_vol": 0}),
        ("vega", {"min_implied_vol": "0.08"}),
        ("vega", {"max_implied_vol": 0.08}),  # == min_implied_vol
        ("vega", {"max_implied_vol": 0.07}),  # below min_implied_vol
        ("vega", {"min_implied_vol": 0.04}),  # below theta's min_implied_vol
        ("vega", {"max_implied_vol": 1.6}),  # above theta's max_implied_vol
        ("vega", {"extra": 1}),
        ("delta", {"min_abs_delta": 0}),
        ("delta", {"min_abs_delta": -0.3}),
        ("delta", {"max_abs_delta": 0.45}),  # == min_abs_delta
        ("delta", {"max_abs_delta": 0.44}),  # below min_abs_delta
        ("delta", {"max_abs_delta": 1.01}),  # no |delta| above 1 is a band
        ("delta", {"max_abs_delta": True}),
        ("delta", {"extra": 1}),
        ("gamma", {"min_hours_to_expiry": 0}),
        ("gamma", {"min_hours_to_expiry": -1}),
        ("gamma", {"min_hours_to_expiry": "6"}),
        ("gamma", {"extra": 1}),
    ],
)
def test_bad_strategy_values_are_rejected(
    strategy_path: Path, tmp_path: Path, section: str, changes: dict[str, Any]
) -> None:
    from footprint.core.config import load_strategy

    data = yaml.safe_load(strategy_path.read_text())
    data[section].update(changes)
    target = tmp_path / "strategy.yaml"
    target.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_strategy(target)


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_loss": 700},  # below stop_min
        {"stop_loss": 1001},  # above stop_max
        {"trail_keep_pct": 100},
        {"trail_keep_pct": 0},
        {"max_contracts": 2},  # scale-out is not built
        {"max_contracts": 0},
        {"fee_per_contract_per_side": -1},
        {"fee_per_contract_per_side": ...},
    ],
)
def test_risk_rejects_bad_stop_trail_and_size(
    risk_path: Path, tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError):
        load_risk(rewrite(risk_path, tmp_path, **changes))


def test_risk_accepts_zero_fees(risk_path: Path, tmp_path: Path) -> None:
    assert (
        load_risk(
            rewrite(risk_path, tmp_path, fee_per_contract_per_side=0)
        ).fee_per_contract_per_side
        == 0
    )


def test_execution_profile_loads(execution_path: Path) -> None:
    from footprint.core.config import load_execution

    e = load_execution(execution_path)
    assert e.quotes.max_age_seconds == 5
    assert e.quotes.greeks_max_age_seconds == 10
    assert (e.contract.max_spread_pct_of_mid, e.contract.min_open_interest) == (Decimal(5), 100)
    assert e.contract.max_strike_distance == Decimal(50)
    assert (e.entry.step_ticks, e.entry.step_seconds, e.entry.max_steps) == (1, 5, 4)
    assert e.entry.timeout_seconds == 30
    assert e.entry.max_over_mid_pct_of_spread == Decimal(25)
    assert e.stop_order.limit_offset_ticks == 20
    assert (e.ibkr.option_security_type, e.ibkr.reconnect_seconds) == ("FOP", 15)


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("quotes", {"max_age_seconds": 0}),
        ("quotes", {"greeks_max_age_seconds": 0}),
        ("contract", {"max_spread_pct_of_mid": 100}),
        ("contract", {"min_open_interest": 0}),  # would switch the filter off
        ("contract", {"max_strike_distance": 0}),
        ("entry", {"step_ticks": 0}),
        ("entry", {"max_over_mid_pct_of_spread": 51}),  # past the ask
        ("entry", {"timeout_seconds": 19}),  # 4 steps x 5 s do not fit
        ("stop_order", {"limit_offset_ticks": 0}),
        ("stop_order", {"extra": 1}),
        ("ibkr", {"reconnect_seconds": 0}),
        # Delta review 3, V16: no wait at all would act at once on a lagging report.
        ("reconcile", {"fill_price_wait_seconds": 0}),
        ("ibkr", {"option_security_type": ""}),
        # Paper only is hard-coded: no key can set it (unknown keys are refused).
        ("ibkr", {"require_paper": False}),
        ("ibkr", {"require_paper": True}),
        ("ibkr", {"paper_account_prefix": "U"}),
        ("ibkr", {"allow_live": True}),
    ],
)
def test_bad_execution_values_are_rejected(
    execution_path: Path, tmp_path: Path, section: str, changes: dict[str, Any]
) -> None:
    from footprint.core.config import load_execution

    data = yaml.safe_load(execution_path.read_text())
    data[section].update(changes)
    target = tmp_path / "execution.yaml"
    target.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_execution(target)


def test_the_reconnect_cap_cannot_be_below_the_first_wait(
    tmp_path: Path, execution_path: Path
) -> None:
    from footprint.core.config import ConfigError, load_execution

    text = execution_path.read_text().replace(
        "max_reconnect_seconds: 300", "max_reconnect_seconds: 5"
    )
    bad = tmp_path / "execution.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="max_reconnect_seconds"):
        load_execution(bad)


def test_the_market_window_must_cover_the_strike_distance_and_the_recentre(
    tmp_path: Path, execution_path: Path
) -> None:
    from footprint.core.config import ConfigError, load_execution

    text = execution_path.read_text().replace(
        "strike_window_points: 100", "strike_window_points: 70"
    )
    bad = tmp_path / "execution.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="strike_window_points"):
        load_execution(bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"early_closes": {date(2026, 7, 4): "10:00"}},  # a Saturday: observed on Friday Jul 3
        {"holidays": [date(2026, 12, 26)]},  # a Saturday
    ],
)
def test_a_calendar_date_on_a_non_trading_day_is_refused(
    instrument_path: Path, tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError, match="not a trading day"):
        load_instrument(rewrite(instrument_path, tmp_path, **changes))


def test_a_key_given_twice_is_refused(instrument_path: Path, tmp_path: Path) -> None:
    text = instrument_path.read_text().replace(
        '  2026-12-24: "10:00"', '  2026-12-24: "10:00"\n  2026-12-24: "13:30"'
    )
    target = tmp_path / "nq.yaml"
    target.write_text(text)
    with pytest.raises(ConfigError, match="duplicate key"):
        load_instrument(target)


def test_a_top_level_key_given_twice_is_refused_in_any_profile(
    execution_path: Path, tmp_path: Path
) -> None:
    from footprint.core.config import load_execution

    target = tmp_path / "execution.yaml"
    target.write_text(execution_path.read_text() + "\nquotes:\n  max_age_seconds: 60\n")
    with pytest.raises(ConfigError, match="duplicate key"):
        load_execution(target)


def test_theta_accepts_a_negative_rate_and_a_full_budget(
    strategy_path: Path, tmp_path: Path
) -> None:
    from footprint.core.config import load_strategy

    data = yaml.safe_load(strategy_path.read_text())
    data["theta"].update({"rate": -0.01, "max_decay_pct_of_target": 100})
    target = tmp_path / "strategy.yaml"
    target.write_text(yaml.safe_dump(data))
    theta = load_strategy(target).theta
    assert (theta.rate, theta.max_decay_pct_of_target) == (Decimal("-0.01"), Decimal(100))


def test_the_greeks_bands_accept_their_edges(strategy_path: Path, tmp_path: Path) -> None:
    from footprint.core.config import load_strategy

    data = yaml.safe_load(strategy_path.read_text())
    theta = data["theta"]
    data["vega"].update(
        {"min_implied_vol": theta["min_implied_vol"], "max_implied_vol": theta["max_implied_vol"]}
    )
    data["delta"].update({"max_abs_delta": 1})
    data["gamma"].update({"min_hours_to_expiry": 0.5})
    target = tmp_path / "strategy.yaml"
    target.write_text(yaml.safe_dump(data))
    s = load_strategy(target)
    assert (s.vega.min_implied_vol, s.vega.max_implied_vol) == (
        s.theta.min_implied_vol,
        s.theta.max_implied_vol,
    )
    assert s.delta.max_abs_delta == 1
    assert s.gamma.min_hours_to_expiry == Decimal("0.5")


def _strategy_with(strategy_path: Path, tmp_path: Path, **sections: dict[str, Any]) -> Path:
    data = yaml.safe_load(strategy_path.read_text())
    for section, changes in sections.items():
        data[section].update(changes)
    target = tmp_path / "strategy.yaml"
    target.write_text(yaml.safe_dump(data))
    return target


@pytest.mark.parametrize(
    ("min_abs_delta", "assumed_delta"),
    [(0.44, 0.45), (0.3, 0.45), (0.45, 0.46), (0.5, 0.6)],
)
def test_the_delta_floor_may_not_be_below_the_assumed_delta(
    strategy_path: Path, tmp_path: Path, min_abs_delta: float, assumed_delta: float
) -> None:
    # Review round 1, M1 (interim until Andre decides GV-5): N, the levels room check,
    # assumes options.assumed_delta. It is only pessimistic while every entry Delta lets
    # through has at least that delta, so the band's floor may not be below it.
    from footprint.core.config import load_strategy

    target = _strategy_with(
        strategy_path,
        tmp_path,
        delta={"min_abs_delta": min_abs_delta},
        options={"assumed_delta": assumed_delta},
    )
    with pytest.raises(ConfigError, match=r"delta: min_abs_delta .* options.assumed_delta"):
        load_strategy(target)


@pytest.mark.parametrize(("min_abs_delta", "assumed_delta"), [(0.45, 0.45), (0.5, 0.45)])
def test_a_delta_floor_at_or_above_the_assumed_delta_loads(
    strategy_path: Path, tmp_path: Path, min_abs_delta: float, assumed_delta: float
) -> None:
    from footprint.core.config import load_strategy

    target = _strategy_with(
        strategy_path,
        tmp_path,
        delta={"min_abs_delta": min_abs_delta},
        options={"assumed_delta": assumed_delta},
    )
    s = load_strategy(target)
    assert s.delta.min_abs_delta == Decimal(str(min_abs_delta))
    assert s.options.assumed_delta == Decimal(str(assumed_delta))


def _profiles_with_gamma(tmp_path: Path, hours: float, dte_max: int | None = None) -> Path:
    import shutil

    from tests.conftest import REPO_ROOT

    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    strategy = config / "strategy.yaml"
    data = yaml.safe_load(strategy.read_text())
    data["gamma"]["min_hours_to_expiry"] = hours
    strategy.write_text(yaml.safe_dump(data))
    if dte_max is not None:
        instrument = config / "instruments" / "nq.yaml"
        raw = yaml.safe_load(instrument.read_text())
        raw["dte_max"] = dte_max
        raw["dte_min"] = min(raw["dte_min"], dte_max)
        instrument.write_text(yaml.safe_dump(raw))
    return config


@pytest.mark.parametrize(("hours", "dte_max"), [(1e30, None), (1e300, None), (96, 3), (24, 0)])
def test_a_gamma_minimum_no_option_could_meet_is_a_config_error(
    tmp_path: Path, hours: float, dte_max: int | None
) -> None:
    # Review round 1, L1: a huge minimum used to crash Pipeline construction with an
    # OverflowError. Felix never takes an option more than dte_max days from the trading
    # day, so a minimum of (dte_max + 1) days or more refuses every entry: a config error.
    from footprint.core.config import load_profiles
    from footprint.decision.pipeline import Pipeline

    config = _profiles_with_gamma(tmp_path, hours, dte_max)
    with pytest.raises(ConfigError, match=r"gamma: min_hours_to_expiry .* dte_max"):
        Pipeline(load_profiles(config, "nq.yaml"))


@pytest.mark.parametrize(("hours", "dte_max"), [(95.99, 3), (23.5, 0)])
def test_a_gamma_minimum_under_the_longest_possible_wait_loads(
    tmp_path: Path, hours: float, dte_max: int
) -> None:
    from footprint.core.config import load_profiles
    from footprint.decision.pipeline import Pipeline

    config = _profiles_with_gamma(tmp_path, hours, dte_max)
    profiles = load_profiles(config, "nq.yaml")
    assert profiles.strategy.gamma.min_hours_to_expiry == Decimal(str(hours))
    Pipeline(profiles)


@pytest.mark.parametrize("section", ["vega", "delta", "gamma"])
def test_a_missing_greeks_section_is_refused(
    strategy_path: Path, tmp_path: Path, section: str
) -> None:
    from footprint.core.config import load_strategy

    data = yaml.safe_load(strategy_path.read_text())
    del data[section]
    target = tmp_path / "strategy.yaml"
    target.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="missing"):
        load_strategy(target)


def test_the_option_expiry_rules_load(instrument_path: Path) -> None:
    rules = load_instrument(instrument_path).option_expiry
    assert rules.time == time(13, 0)
    assert rules.early_close_time == time(10, 0)
    assert rules.early_close_dates == frozenset({date(2026, 11, 27), date(2026, 12, 24)})
    assert rules.timezone == "America/Los_Angeles"
    assert rules.american_trading_classes == frozenset({"NQ"})
    assert rules.european_trading_class_patterns == ("Q[1-5][A-D]", "QN[1-5]", "QNE")


def expiry(**changes: Any) -> dict[str, Any]:
    return {
        "time": "13:00",
        "early_close_time": "10:00",
        "early_close_dates": [date(2026, 11, 27), date(2026, 12, 24)],
        "timezone": "America/Los_Angeles",
        "american_trading_classes": ["NQ"],
        "european_trading_class_patterns": ["Q[1-5][A-D]", "QN[1-5]", "QNE"],
    } | changes


@pytest.mark.parametrize(
    "rules",
    [
        expiry(time="13:00:30"),
        expiry(time="1pm"),
        expiry(time="13:00+05:00"),  # an offset would be silently dropped
        expiry(early_close_time="13:00"),  # not before the normal time
        expiry(early_close_time="14:00"),
        expiry(early_close_time="09:00-05:00"),
        expiry(early_close_dates=[date(2026, 11, 27), date(2026, 11, 27)]),
        expiry(early_close_dates=[]),  # the day after Thanksgiving is missing
        expiry(early_close_dates=[date(2026, 11, 27)]),  # Christmas Eve is missing
        expiry(early_close_dates=[date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)]),
        expiry(early_close_dates=["2026-11-27x"]),
        expiry(early_close_dates=[date(2026, 11, 28)]),  # a Saturday
        expiry(timezone="Mars/Olympus"),
        expiry(timezone="/etc/localtime"),  # malformed key: ValueError inside zoneinfo
        expiry(american_trading_classes=[]),
        expiry(american_trading_classes="NQ"),  # a list, not a string
        expiry(american_trading_classes=[""]),
        expiry(american_trading_classes=["NQ "]),  # IBKR's classes have no spaces
        expiry(american_trading_classes=["NQ", "NQ"]),
        expiry(american_trading_classes=[1]),
        expiry(american_trading_classes=[True]),
        expiry(american_trading_classes=None),
        expiry(european_trading_class_patterns=[]),
        expiry(european_trading_class_patterns="QNE"),  # a list, not a string
        expiry(european_trading_class_patterns=[""]),
        expiry(european_trading_class_patterns=["QN[1-5"]),  # not a valid pattern
        expiry(european_trading_class_patterns=["QN[1-5]", "QN[1-5]"]),
        expiry(european_trading_class_patterns=[3]),
        expiry(european_trading_class_patterns=["N."]),  # matches the American NQ
        expiry(european_trading_class_patterns=["Q N"]),
        expiry(extra=1),
        {k: v for k, v in expiry().items() if k != "time"},
    ],
)
def test_bad_option_expiry_rules_are_rejected(
    instrument_path: Path, tmp_path: Path, rules: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError, match="option_expiry"):
        load_instrument(rewrite(instrument_path, tmp_path, option_expiry=rules))


def test_several_american_classes_load(instrument_path: Path, tmp_path: Path) -> None:
    rules = expiry(american_trading_classes=["NQ", "XQ"])
    profile = load_instrument(rewrite(instrument_path, tmp_path, option_expiry=rules))
    assert profile.option_expiry.american_trading_classes == frozenset({"NQ", "XQ"})


def test_the_old_date_rule_keys_are_refused(instrument_path: Path, tmp_path: Path) -> None:
    # The quarterly-by-date rule was wrong (a weekly is listed on quarterly Fridays): a
    # config still carrying it must not load as if it meant something.
    rules = expiry(american_months=[3, 6, 9, 12])
    with pytest.raises(ConfigError, match="option_expiry"):
        load_instrument(rewrite(instrument_path, tmp_path, option_expiry=rules))


def test_an_option_early_close_outside_the_calendar_names_the_window(
    instrument_path: Path, tmp_path: Path
) -> None:
    rules = expiry(early_close_dates=[date(2025, 12, 24), date(2026, 11, 27), date(2026, 12, 24)])
    with pytest.raises(ConfigError, match="outside calendar_from"):
        load_instrument(rewrite(instrument_path, tmp_path, option_expiry=rules))


def test_a_missing_option_early_close_names_the_rule(instrument_path: Path, tmp_path: Path) -> None:
    rules = expiry(early_close_dates=[date(2026, 12, 24)])
    with pytest.raises(ConfigError, match=r"lacks 2026-11-27 \(Day after Thanksgiving"):
        load_instrument(rewrite(instrument_path, tmp_path, option_expiry=rules))


def test_a_no_trade_day_does_not_excuse_a_missing_option_early_close(
    instrument_path: Path, tmp_path: Path
) -> None:
    # No-trade days are Andre's own choice; the stock market still closes early on them,
    # so the options still stop early (review round 3, R3-1).
    data = yaml.safe_load(instrument_path.read_text())
    no_trade = [*data["no_trade_days"], date(2026, 11, 27)]
    early = {d: t for d, t in data["early_closes"].items() if d != date(2026, 11, 27)}
    rules = expiry(early_close_dates=[date(2026, 12, 24)])
    path = rewrite(
        instrument_path,
        tmp_path,
        no_trade_days=no_trade,
        early_closes=early,
        option_expiry=rules,
    )
    with pytest.raises(ConfigError, match=r"option_expiry: early_close_dates lacks 2026-11-27"):
        load_instrument(path)


def test_a_weekend_date_error_names_its_list(instrument_path: Path, tmp_path: Path) -> None:
    rules = expiry(early_close_dates=[date(2026, 11, 27), date(2026, 11, 28), date(2026, 12, 24)])
    with pytest.raises(ConfigError, match="option_expiry early_close_dates 2026-11-28"):
        load_instrument(rewrite(instrument_path, tmp_path, option_expiry=rules))
