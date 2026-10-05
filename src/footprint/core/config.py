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
    cash_session: Session
    option_tick_size: Decimal
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
        cash_session=_session(raw, "cash_session", where),
        option_tick_size=_positive_decimal(raw, "option_tick_size", where),
        dte_min=_integer(raw, "dte_min", where, minimum=0),
        dte_max=_integer(raw, "dte_max", where, minimum=0),
        trading_days=_trading_days(raw, "trading_days", where),
        holidays=_holidays(raw, "holidays", where),
    )
    if profile.prime_session.start > profile.prime_session.end:
        raise ConfigError(f"{where}: prime_session must not cross midnight")
    if profile.prime_session.timezone != profile.overnight_session.timezone:
        raise ConfigError(
            f"{where}: prime_session and overnight_session must use the same timezone "
            "so they can be checked for overlap"
        )
    if profile.prime_session.overlaps(profile.overnight_session):
        raise ConfigError(f"{where}: prime_session and overnight_session must not overlap")
    cash, prime = profile.cash_session, profile.prime_session
    if cash.timezone != prime.timezone:
        raise ConfigError(f"{where}: cash_session must use the prime_session timezone")
    if not (prime.start <= cash.start < cash.end <= prime.end):
        raise ConfigError(f"{where}: cash_session must lie inside prime_session")
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


@dataclass(frozen=True, slots=True)
class TrendSettings:
    timeframe_minutes: int
    ema_fast: int
    ema_slow: int
    swing_strength: int


@dataclass(frozen=True, slots=True)
class VolumeSettings:
    timeframe_minutes: int
    lookback_sessions: int
    spike_multiple: Decimal


@dataclass(frozen=True, slots=True)
class RangeSettings:
    adr_days: int
    quiet_below_pct: Decimal
    extended_above_pct: Decimal
    chase_zone_pct: Decimal


@dataclass(frozen=True, slots=True)
class LevelsSettings:
    room_multiple: Decimal
    wall_multiple: Decimal
    swing_lookback_bars: int


@dataclass(frozen=True, slots=True)
class OptionsSettings:
    assumed_delta: Decimal


@dataclass(frozen=True, slots=True)
class ZeroDaySettings:
    lookback_sessions: int
    trip_multiple: Decimal
    volume_trip_multiple: Decimal


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    trend: TrendSettings
    volume: VolumeSettings
    range: RangeSettings
    levels: LevelsSettings
    options: OptionsSettings
    zeroday: ZeroDaySettings


def load_strategy(path: Path) -> StrategyProfile:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(StrategyProfile)})

    t_where = f"{where}: trend"
    t = _check_keys(raw["trend"], {f.name for f in fields(TrendSettings)}, t_where)
    trend = TrendSettings(
        timeframe_minutes=_integer(t, "timeframe_minutes", t_where, minimum=1),
        ema_fast=_integer(t, "ema_fast", t_where, minimum=1),
        ema_slow=_integer(t, "ema_slow", t_where, minimum=1),
        swing_strength=_integer(t, "swing_strength", t_where, minimum=1),
    )
    if trend.ema_fast >= trend.ema_slow:
        raise ConfigError(f"{t_where}: ema_fast must be < ema_slow")

    v_where = f"{where}: volume"
    v = _check_keys(raw["volume"], {f.name for f in fields(VolumeSettings)}, v_where)
    volume = VolumeSettings(
        timeframe_minutes=_integer(v, "timeframe_minutes", v_where, minimum=1),
        lookback_sessions=_integer(v, "lookback_sessions", v_where, minimum=1),
        spike_multiple=_positive_decimal(v, "spike_multiple", v_where),
    )
    if volume.spike_multiple <= 1:
        raise ConfigError(f"{v_where}: spike_multiple must be > 1")

    r_where = f"{where}: range"
    r = _check_keys(raw["range"], {f.name for f in fields(RangeSettings)}, r_where)
    range_ = RangeSettings(
        adr_days=_integer(r, "adr_days", r_where, minimum=1),
        quiet_below_pct=_positive_decimal(r, "quiet_below_pct", r_where),
        extended_above_pct=_positive_decimal(r, "extended_above_pct", r_where),
        chase_zone_pct=_positive_decimal(r, "chase_zone_pct", r_where),
    )
    if range_.quiet_below_pct >= range_.extended_above_pct:
        raise ConfigError(f"{r_where}: quiet_below_pct must be < extended_above_pct")
    if range_.chase_zone_pct >= _PERCENT_MAX:
        raise ConfigError(f"{r_where}: chase_zone_pct must be < {_PERCENT_MAX}")

    l_where = f"{where}: levels"
    lv = _check_keys(raw["levels"], {f.name for f in fields(LevelsSettings)}, l_where)
    levels = LevelsSettings(
        room_multiple=_positive_decimal(lv, "room_multiple", l_where),
        wall_multiple=_positive_decimal(lv, "wall_multiple", l_where),
        swing_lookback_bars=_integer(lv, "swing_lookback_bars", l_where, minimum=1),
    )
    if levels.wall_multiple > levels.room_multiple:
        raise ConfigError(f"{l_where}: wall_multiple must be <= room_multiple")

    o_where = f"{where}: options"
    o = _check_keys(raw["options"], {f.name for f in fields(OptionsSettings)}, o_where)
    options = OptionsSettings(assumed_delta=_positive_decimal(o, "assumed_delta", o_where))
    if options.assumed_delta >= 1:
        raise ConfigError(f"{o_where}: assumed_delta must be < 1")

    z_where = f"{where}: zeroday"
    z = _check_keys(raw["zeroday"], {f.name for f in fields(ZeroDaySettings)}, z_where)
    zeroday = ZeroDaySettings(
        lookback_sessions=_integer(z, "lookback_sessions", z_where, minimum=1),
        trip_multiple=_positive_decimal(z, "trip_multiple", z_where),
        volume_trip_multiple=_positive_decimal(z, "volume_trip_multiple", z_where),
    )
    if zeroday.trip_multiple <= 1 or zeroday.volume_trip_multiple <= 1:
        raise ConfigError(f"{z_where}: trip multiples must be > 1")

    return StrategyProfile(
        trend=trend, volume=volume, range=range_, levels=levels, options=options, zeroday=zeroday
    )
