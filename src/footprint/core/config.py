"""Loads and validates the YAML profiles under config/. Bad values raise ConfigError."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

_PERCENT_MAX = Decimal(100)  # allow-literal: upper bound of a percentage


class ConfigError(ValueError):
    """A profile is missing, malformed or contains an invalid value."""


@dataclass(frozen=True, slots=True)
class Session:
    """A daily trading window, `start` and `end` both inclusive.

    `end` earlier than `start` means it crosses midnight.
    """

    start: time
    end: time
    timezone: str

    def _covers(self, moment: time) -> bool:
        if self.start <= self.end:
            return self.start <= moment <= self.end
        return moment >= self.start or moment <= self.end

    def overlaps(self, other: Session) -> bool:
        """True if the two windows share any time of day. Both must use one timezone."""
        if self.timezone != other.timezone:
            raise ValueError(f"cannot compare sessions in {self.timezone} and {other.timezone}")
        return self._covers(other.start) or other._covers(self.start)


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    symbol: str
    exchange: str
    point_value: Decimal
    tick_size: Decimal
    round_number_step: Decimal
    prime_session: Session
    overnight_session: Session
    dte_min: int
    dte_max: int
    trading_days: frozenset[int]
    holidays: frozenset[date]


@dataclass(frozen=True, slots=True)
class RiskProfile:
    bankroll: Decimal
    position_cap: Decimal
    overnight_position_cap: Decimal
    profit_target: Decimal
    stop_min: Decimal
    stop_max: Decimal
    daily_max_stopouts: int
    daily_max_loss: Decimal
    weekly_max_loss: Decimal
    kill_switch_drawdown_pct: Decimal
    max_trades_prime: int
    max_trades_overnight: int


def _read_mapping(path: Path, expected: set[str]) -> Mapping[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read profile: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return _check_keys(raw, expected, str(path))


def _check_keys(raw: object, expected: set[str], where: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(raw).__name__}")
    keys = set(raw)
    if missing := expected - keys:
        raise ConfigError(f"{where}: missing keys: {sorted(missing)}")
    if unknown := keys - expected:
        raise ConfigError(f"{where}: unknown keys: {sorted(map(str, unknown))}")
    return raw


def _text(raw: Mapping[str, object], key: str, where: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: {key} must be a non-empty string, got {value!r}")
    return value


def _positive_decimal(raw: Mapping[str, object], key: str, where: str) -> Decimal:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ConfigError(f"{where}: {key} must be a finite number > 0, got {value!r}")
    return number


def _integer(raw: Mapping[str, object], key: str, where: str, *, minimum: int) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {key} must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where}: {key} must be >= {minimum}, got {value}")
    return value


def _session(raw: Mapping[str, object], key: str, where: str) -> Session:
    where = f"{where}: {key}"
    body = _check_keys(raw[key], {f.name for f in fields(Session)}, where)
    clock: dict[str, time] = {}
    for edge in ("start", "end"):
        text = _text(body, edge, where)
        try:
            parsed = time.fromisoformat(text)
        except ValueError as exc:
            raise ConfigError(f"{where}: {edge} must be HH:MM, got {text!r}") from exc
        if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
            raise ConfigError(f"{where}: {edge} must be HH:MM with no seconds, got {text!r}")
        clock[edge] = parsed
    if clock["start"] == clock["end"]:
        raise ConfigError(f"{where}: start and end must differ")
    timezone = _text(body, "timezone", where)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{where}: unknown timezone {timezone!r}") from exc
    return Session(start=clock["start"], end=clock["end"], timezone=timezone)


_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _trading_days(raw: Mapping[str, object], key: str, where: str) -> frozenset[int]:
    value = raw[key]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: {key} must be a non-empty list of weekday names")
    days: set[int] = set()
    for item in value:
        if not isinstance(item, str) or item not in _WEEKDAYS:
            raise ConfigError(f"{where}: {key} entries must be one of {_WEEKDAYS}, got {item!r}")
        days.add(_WEEKDAYS.index(item))
    if len(days) != len(value):
        raise ConfigError(f"{where}: {key} has duplicate days")
    return frozenset(days)


def _holidays(raw: Mapping[str, object], key: str, where: str) -> frozenset[date]:
    value = raw[key]
    if not isinstance(value, list):
        raise ConfigError(f"{where}: {key} must be a list of dates (YYYY-MM-DD)")
    days: set[date] = set()
    for item in value:
        if not isinstance(item, date) or isinstance(item, datetime):
            raise ConfigError(f"{where}: {key} entries must be dates (YYYY-MM-DD), got {item!r}")
        days.add(item)
    if len(days) != len(value):
        raise ConfigError(f"{where}: {key} has duplicate dates")
    return frozenset(days)


def load_instrument(path: Path) -> InstrumentProfile:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(InstrumentProfile)})
    profile = InstrumentProfile(
        symbol=_text(raw, "symbol", where),
        exchange=_text(raw, "exchange", where),
        point_value=_positive_decimal(raw, "point_value", where),
        tick_size=_positive_decimal(raw, "tick_size", where),
        round_number_step=_positive_decimal(raw, "round_number_step", where),
        prime_session=_session(raw, "prime_session", where),
        overnight_session=_session(raw, "overnight_session", where),
        dte_min=_integer(raw, "dte_min", where, minimum=0),
        dte_max=_integer(raw, "dte_max", where, minimum=0),
        trading_days=_trading_days(raw, "trading_days", where),
        holidays=_holidays(raw, "holidays", where),
    )
    if profile.prime_session.timezone != profile.overnight_session.timezone:
        raise ConfigError(
            f"{where}: prime_session and overnight_session must use the same timezone "
            "so they can be checked for overlap"
        )
    if profile.prime_session.overlaps(profile.overnight_session):
        raise ConfigError(f"{where}: prime_session and overnight_session must not overlap")
    if profile.round_number_step % profile.tick_size != 0:
        raise ConfigError(f"{where}: round_number_step must be a multiple of tick_size")
    if profile.dte_min > profile.dte_max:
        raise ConfigError(f"{where}: dte_min must be <= dte_max")
    return profile


def load_risk(path: Path) -> RiskProfile:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(RiskProfile)})
    profile = RiskProfile(
        bankroll=_positive_decimal(raw, "bankroll", where),
        position_cap=_positive_decimal(raw, "position_cap", where),
        overnight_position_cap=_positive_decimal(raw, "overnight_position_cap", where),
        profit_target=_positive_decimal(raw, "profit_target", where),
        stop_min=_positive_decimal(raw, "stop_min", where),
        stop_max=_positive_decimal(raw, "stop_max", where),
        daily_max_stopouts=_integer(raw, "daily_max_stopouts", where, minimum=1),
        daily_max_loss=_positive_decimal(raw, "daily_max_loss", where),
        weekly_max_loss=_positive_decimal(raw, "weekly_max_loss", where),
        kill_switch_drawdown_pct=_positive_decimal(raw, "kill_switch_drawdown_pct", where),
        max_trades_prime=_integer(raw, "max_trades_prime", where, minimum=0),
        max_trades_overnight=_integer(raw, "max_trades_overnight", where, minimum=0),
    )
    if profile.position_cap > profile.bankroll:
        raise ConfigError(f"{where}: position_cap must be <= bankroll")
    if profile.overnight_position_cap > profile.position_cap:
        raise ConfigError(f"{where}: overnight_position_cap must be <= position_cap")
    if profile.stop_min > profile.stop_max:
        raise ConfigError(f"{where}: stop_min must be <= stop_max")
    if profile.daily_max_loss > profile.weekly_max_loss:
        raise ConfigError(f"{where}: daily_max_loss must be <= weekly_max_loss")
    if profile.kill_switch_drawdown_pct > _PERCENT_MAX:
        raise ConfigError(f"{where}: kill_switch_drawdown_pct must be <= {_PERCENT_MAX}")
    return profile
