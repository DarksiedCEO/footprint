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
    target = rewrite(instrument_path, tmp_path, holidays=[date(2026, 12, 25)])
    assert load_instrument(target).holidays == frozenset({date(2026, 12, 25)})


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
        ("contract", {"max_spread_pct_of_mid": 100}),
        ("contract", {"min_open_interest": 0}),  # would switch the filter off
        ("contract", {"max_strike_distance": 0}),
        ("entry", {"step_ticks": 0}),
        ("entry", {"max_over_mid_pct_of_spread": 51}),  # past the ask
        ("entry", {"timeout_seconds": 19}),  # 4 steps x 5 s do not fit
        ("stop_order", {"limit_offset_ticks": 0}),
        ("stop_order", {"extra": 1}),
        ("ibkr", {"reconnect_seconds": 0}),
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
