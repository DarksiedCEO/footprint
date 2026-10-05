from datetime import time
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
    with pytest.raises(ConfigError, match="must not overlap"):
        load_instrument(rewrite(instrument_path, tmp_path, **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"overnight_session": overnight(start="14:01")},  # adjacent to prime's end
        {"overnight_session": overnight(start="22:00", end="23:00")},
        {"prime_session": session(start="04:00", end="04:30")},
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
