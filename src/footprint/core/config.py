"""Loads and validates the YAML profiles under config/. Bad values raise ConfigError."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
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
    currency: str
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
    early_closes: Mapping[date, time]  # prime session ends early (prime_session timezone)
    calendar_through: date  # holidays and early closes are complete up to this date
    calendar_warn_days: int  # say so this many days before the calendar runs out


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
    stop_loss: Decimal
    trail_keep_pct: Decimal
    max_contracts: int
    fee_per_contract_per_side: Decimal


class _UniqueKeyLoader(yaml.SafeLoader):
    """safe_load, but a key given twice in one mapping is an error (plain YAML keeps the
    last one silently: one stray line could move an early close or a limit)."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[object, object]:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,
)


def load_yaml(text: str) -> object:
    """Every profile is read through this: safe, and duplicate keys refused."""
    return yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass: safe


def _read_mapping(path: Path, expected: set[str]) -> Mapping[str, object]:
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
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


def _boolean(raw: Mapping[str, object], key: str, where: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false, got {value!r}")
    return value


def _positive_decimal(raw: Mapping[str, object], key: str, where: str) -> Decimal:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ConfigError(f"{where}: {key} must be a finite number > 0, got {value!r}")
    return number


def _non_negative_decimal(raw: Mapping[str, object], key: str, where: str) -> Decimal:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ConfigError(f"{where}: {key} must be a finite number >= 0, got {value!r}")
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


def _early_closes(raw: Mapping[str, object], key: str, where: str) -> Mapping[date, time]:
    value = raw[key]
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: {key} must be a mapping of YYYY-MM-DD: HH:MM")
    closes: dict[date, time] = {}
    for day, text in value.items():
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ConfigError(f"{where}: {key} keys must be dates (YYYY-MM-DD), got {day!r}")
        try:
            parsed = time.fromisoformat(text) if isinstance(text, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.second or parsed.microsecond or parsed.tzinfo is not None:
            raise ConfigError(f"{where}: {key} {day} must be HH:MM, got {text!r}")
        closes[day] = parsed
    return MappingProxyType(closes)


def _date(raw: Mapping[str, object], key: str, where: str) -> date:
    value = raw[key]
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ConfigError(f"{where}: {key} must be a date (YYYY-MM-DD), got {value!r}")
    return value


def load_instrument(path: Path) -> InstrumentProfile:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(InstrumentProfile)})
    profile = InstrumentProfile(
        symbol=_text(raw, "symbol", where),
        exchange=_text(raw, "exchange", where),
        currency=_text(raw, "currency", where),
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
        early_closes=_early_closes(raw, "early_closes", where),
        calendar_through=_date(raw, "calendar_through", where),
        calendar_warn_days=_integer(raw, "calendar_warn_days", where, minimum=1),
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
    for day in (*profile.holidays, *profile.early_closes):
        if day.weekday() not in profile.trading_days:
            raise ConfigError(
                f"{where}: {day} is a {day:%A}, not a trading day: a holiday or early close "
                "goes on the weekday the exchange observes it (e.g. Friday for a Saturday)"
            )
    for day, close in profile.early_closes.items():
        if day in profile.holidays:
            raise ConfigError(f"{where}: {day} is both a holiday and an early close")
        if not (cash.start < close < prime.end):
            raise ConfigError(
                f"{where}: early close {day} {close} must be after the cash open and "
                "before the normal prime close"
            )
    late = [d for d in (*profile.holidays, *profile.early_closes) if d > profile.calendar_through]
    if late:
        raise ConfigError(f"{where}: {sorted(late)[0]} is after calendar_through")
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
        stop_loss=_positive_decimal(raw, "stop_loss", where),
        trail_keep_pct=_positive_decimal(raw, "trail_keep_pct", where),
        max_contracts=_integer(raw, "max_contracts", where, minimum=1),
        fee_per_contract_per_side=_non_negative_decimal(raw, "fee_per_contract_per_side", where),
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
    if not profile.stop_min <= profile.stop_loss <= profile.stop_max:
        raise ConfigError(f"{where}: stop_loss must lie within stop_min..stop_max")
    if profile.trail_keep_pct >= _PERCENT_MAX:
        raise ConfigError(f"{where}: trail_keep_pct must be < {_PERCENT_MAX}")
    if profile.max_contracts != 1:
        raise ConfigError(
            f"{where}: max_contracts must be 1 in v1 (Option A); scale-out is not built"
        )
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
class DataSettings:
    max_bar_age_seconds: int
    min_session_coverage_pct: Decimal
    max_gap_minutes: int


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
    recent_bars: int
    spike_bar_trip_multiple: Decimal


@dataclass(frozen=True, slots=True)
class ConsoleSettings:
    levels_weight: int
    range_weight: int
    fire_threshold: int
    close_lead_minutes: int


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    trend: TrendSettings
    volume: VolumeSettings
    range: RangeSettings
    data: DataSettings
    levels: LevelsSettings
    options: OptionsSettings
    zeroday: ZeroDaySettings
    console: ConsoleSettings


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
    if levels.swing_lookback_bars < 2 * trend.swing_strength + 1:
        raise ConfigError(
            f"{l_where}: swing_lookback_bars must be >= 2 * trend.swing_strength + 1 "
            "or no swing can ever be found"
        )

    d_where = f"{where}: data"
    dd = _check_keys(raw["data"], {f.name for f in fields(DataSettings)}, d_where)
    data = DataSettings(
        max_bar_age_seconds=_integer(dd, "max_bar_age_seconds", d_where, minimum=1),
        min_session_coverage_pct=_positive_decimal(dd, "min_session_coverage_pct", d_where),
        max_gap_minutes=_integer(dd, "max_gap_minutes", d_where, minimum=0),
    )
    if data.min_session_coverage_pct > _PERCENT_MAX:
        raise ConfigError(f"{d_where}: min_session_coverage_pct must be <= {_PERCENT_MAX}")

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
        recent_bars=_integer(z, "recent_bars", z_where, minimum=2),
        spike_bar_trip_multiple=_positive_decimal(z, "spike_bar_trip_multiple", z_where),
    )
    if zeroday.spike_bar_trip_multiple < zeroday.trip_multiple:
        raise ConfigError(f"{z_where}: spike_bar_trip_multiple must be >= trip_multiple")
    if zeroday.trip_multiple <= 1 or zeroday.volume_trip_multiple <= 1:
        raise ConfigError(f"{z_where}: trip multiples must be > 1")

    k_where = f"{where}: console"
    k = _check_keys(raw["console"], {f.name for f in fields(ConsoleSettings)}, k_where)
    console = ConsoleSettings(
        levels_weight=_integer(k, "levels_weight", k_where, minimum=1),
        range_weight=_integer(k, "range_weight", k_where, minimum=1),
        fire_threshold=_integer(k, "fire_threshold", k_where, minimum=1),
        close_lead_minutes=_integer(k, "close_lead_minutes", k_where, minimum=0),
    )
    if console.fire_threshold > console.levels_weight + console.range_weight:
        raise ConfigError(f"{k_where}: fire_threshold is above the highest possible score")
    if console.fire_threshold <= max(console.levels_weight, console.range_weight):
        raise ConfigError(
            f"{k_where}: fire_threshold must need both confirmers (above each weight alone)"
        )

    return StrategyProfile(
        trend=trend,
        volume=volume,
        range=range_,
        data=data,
        levels=levels,
        options=options,
        zeroday=zeroday,
        console=console,
    )


@dataclass(frozen=True, slots=True)
class QuoteSettings:
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class ContractSettings:
    max_spread_pct_of_mid: Decimal
    min_open_interest: int
    max_strike_distance: Decimal


@dataclass(frozen=True, slots=True)
class EntrySettings:
    step_ticks: int
    step_seconds: int
    max_steps: int
    timeout_seconds: int
    max_over_mid_pct_of_spread: Decimal


@dataclass(frozen=True, slots=True)
class StopOrderSettings:
    limit_offset_ticks: int


@dataclass(frozen=True, slots=True)
class NewsSettings:
    time_stop_minutes: int
    time_stop_min_gain: Decimal
    window_minutes: int


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    max_unmanaged_seconds: int


@dataclass(frozen=True, slots=True)
class ReconcileSettings:
    settle_seconds: int
    max_settle_seconds: int
    cancel_escalate_seconds: int


@dataclass(frozen=True, slots=True)
class IbkrConfig:
    ack_timeout_seconds: int
    positions_disagree_alarm_seconds: int
    resync_realarm_seconds: int
    reconnect_seconds: int
    option_security_type: str
    require_paper: bool
    paper_account_prefix: str
    escalate_after_failures: int
    max_reconnect_seconds: int
    min_healthy_session_seconds: int


@dataclass(frozen=True, slots=True)
class MarketDataSettings:
    history_days: int
    roll_days_before_expiry: int
    bar_poll_delay_seconds: int
    min_poll_seconds: int
    history_request_gap_seconds: int
    request_timeout_seconds: int
    retry_seconds: int
    strike_window_points: Decimal
    recenter_points: Decimal
    max_quote_lines: int


@dataclass(frozen=True, slots=True)
class AlarmSettings:
    retry_seconds: int
    max_pending: int
    channel_timeout_seconds: int
    call_cooldown_seconds: int


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    quotes: QuoteSettings
    contract: ContractSettings
    entry: EntrySettings
    stop_order: StopOrderSettings
    news: NewsSettings
    runner: RunnerSettings
    reconcile: ReconcileSettings
    alarms: AlarmSettings
    ibkr: IbkrConfig
    market_data: MarketDataSettings


def load_execution(path: Path) -> ExecutionProfile:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(ExecutionProfile)})

    q_where = f"{where}: quotes"
    q = _check_keys(raw["quotes"], {f.name for f in fields(QuoteSettings)}, q_where)
    quotes = QuoteSettings(max_age_seconds=_integer(q, "max_age_seconds", q_where, minimum=1))

    c_where = f"{where}: contract"
    c = _check_keys(raw["contract"], {f.name for f in fields(ContractSettings)}, c_where)
    contract = ContractSettings(
        max_spread_pct_of_mid=_positive_decimal(c, "max_spread_pct_of_mid", c_where),
        min_open_interest=_integer(c, "min_open_interest", c_where, minimum=1),
        max_strike_distance=_positive_decimal(c, "max_strike_distance", c_where),
    )
    if contract.max_spread_pct_of_mid >= _PERCENT_MAX:
        raise ConfigError(f"{c_where}: max_spread_pct_of_mid must be < {_PERCENT_MAX}")

    e_where = f"{where}: entry"
    e = _check_keys(raw["entry"], {f.name for f in fields(EntrySettings)}, e_where)
    entry = EntrySettings(
        step_ticks=_integer(e, "step_ticks", e_where, minimum=1),
        step_seconds=_integer(e, "step_seconds", e_where, minimum=1),
        max_steps=_integer(e, "max_steps", e_where, minimum=0),
        timeout_seconds=_integer(e, "timeout_seconds", e_where, minimum=1),
        max_over_mid_pct_of_spread=_non_negative_decimal(e, "max_over_mid_pct_of_spread", e_where),
    )
    if entry.max_over_mid_pct_of_spread > _PERCENT_MAX / 2:
        raise ConfigError(
            f"{e_where}: max_over_mid_pct_of_spread must be <= half the spread (the ask)"
        )
    if entry.step_seconds * entry.max_steps > entry.timeout_seconds:
        raise ConfigError(f"{e_where}: all steps must fit inside timeout_seconds")

    s_where = f"{where}: stop_order"
    so = _check_keys(raw["stop_order"], {f.name for f in fields(StopOrderSettings)}, s_where)
    stop_order = StopOrderSettings(
        limit_offset_ticks=_integer(so, "limit_offset_ticks", s_where, minimum=1)
    )
    n_where = f"{where}: news"
    n = _check_keys(raw["news"], {f.name for f in fields(NewsSettings)}, n_where)
    news = NewsSettings(
        time_stop_minutes=_integer(n, "time_stop_minutes", n_where, minimum=1),
        time_stop_min_gain=_positive_decimal(n, "time_stop_min_gain", n_where),
        window_minutes=_integer(n, "window_minutes", n_where, minimum=0),
    )
    r_where = f"{where}: runner"
    r = _check_keys(raw["runner"], {f.name for f in fields(RunnerSettings)}, r_where)
    runner = RunnerSettings(
        max_unmanaged_seconds=_integer(r, "max_unmanaged_seconds", r_where, minimum=1)
    )
    rc_where = f"{where}: reconcile"
    rc = _check_keys(raw["reconcile"], {f.name for f in fields(ReconcileSettings)}, rc_where)
    reconcile = ReconcileSettings(
        settle_seconds=_integer(rc, "settle_seconds", rc_where, minimum=0),
        max_settle_seconds=_integer(rc, "max_settle_seconds", rc_where, minimum=1),
        cancel_escalate_seconds=_integer(rc, "cancel_escalate_seconds", rc_where, minimum=1),
    )
    if reconcile.max_settle_seconds <= reconcile.settle_seconds:
        raise ConfigError(f"{rc_where}: max_settle_seconds must exceed settle_seconds")
    a_where = f"{where}: alarms"
    al = _check_keys(raw["alarms"], {f.name for f in fields(AlarmSettings)}, a_where)
    alarms = AlarmSettings(
        retry_seconds=_integer(al, "retry_seconds", a_where, minimum=1),
        max_pending=_integer(al, "max_pending", a_where, minimum=1),
        channel_timeout_seconds=_integer(al, "channel_timeout_seconds", a_where, minimum=1),
        call_cooldown_seconds=_integer(al, "call_cooldown_seconds", a_where, minimum=0),
    )
    if alarms.channel_timeout_seconds >= alarms.retry_seconds * 2:
        raise ConfigError(
            f"{a_where}: channel_timeout_seconds must be under twice retry_seconds, or a "
            "hung channel holds back the fallback too long"
        )
    i_where = f"{where}: ibkr"
    ib = _check_keys(raw["ibkr"], {f.name for f in fields(IbkrConfig)}, i_where)
    ibkr = IbkrConfig(
        ack_timeout_seconds=_integer(ib, "ack_timeout_seconds", i_where, minimum=1),
        positions_disagree_alarm_seconds=_integer(
            ib, "positions_disagree_alarm_seconds", i_where, minimum=1
        ),
        resync_realarm_seconds=_integer(ib, "resync_realarm_seconds", i_where, minimum=1),
        reconnect_seconds=_integer(ib, "reconnect_seconds", i_where, minimum=1),
        option_security_type=_text(ib, "option_security_type", i_where),
        require_paper=_boolean(ib, "require_paper", i_where),
        paper_account_prefix=_text(ib, "paper_account_prefix", i_where),
        escalate_after_failures=_integer(ib, "escalate_after_failures", i_where, minimum=1),
        max_reconnect_seconds=_integer(ib, "max_reconnect_seconds", i_where, minimum=1),
        min_healthy_session_seconds=_integer(ib, "min_healthy_session_seconds", i_where, minimum=0),
    )
    if ibkr.max_reconnect_seconds < ibkr.reconnect_seconds:
        raise ConfigError(f"{i_where}: max_reconnect_seconds must be >= reconnect_seconds")
    m_where = f"{where}: market_data"
    m = _check_keys(raw["market_data"], {f.name for f in fields(MarketDataSettings)}, m_where)
    market_data = MarketDataSettings(
        history_days=_integer(m, "history_days", m_where, minimum=1),
        roll_days_before_expiry=_integer(m, "roll_days_before_expiry", m_where, minimum=1),
        bar_poll_delay_seconds=_integer(m, "bar_poll_delay_seconds", m_where, minimum=1),
        min_poll_seconds=_integer(m, "min_poll_seconds", m_where, minimum=1),
        history_request_gap_seconds=_integer(m, "history_request_gap_seconds", m_where, minimum=1),
        request_timeout_seconds=_integer(m, "request_timeout_seconds", m_where, minimum=1),
        retry_seconds=_integer(m, "retry_seconds", m_where, minimum=1),
        strike_window_points=_positive_decimal(m, "strike_window_points", m_where),
        recenter_points=_positive_decimal(m, "recenter_points", m_where),
        max_quote_lines=_integer(m, "max_quote_lines", m_where, minimum=2),
    )
    if (
        market_data.strike_window_points
        < contract.max_strike_distance + market_data.recenter_points
    ):
        raise ConfigError(
            f"{m_where}: strike_window_points must cover contract.max_strike_distance plus "
            "recenter_points, or the strike the picker wants may not be streamed"
        )
    return ExecutionProfile(
        quotes=quotes,
        contract=contract,
        entry=entry,
        stop_order=stop_order,
        news=news,
        runner=runner,
        reconcile=reconcile,
        alarms=alarms,
        ibkr=ibkr,
        market_data=market_data,
    )


def check_consistency(
    instrument: InstrumentProfile, risk: RiskProfile, execution: ExecutionProfile
) -> None:
    """Rules that span more than one profile. Raises ConfigError.

    The worst fill of the resting stop-limit (its limit price) plus the round-trip
    fees must stay within stop_max, so the "wide limit" (G1) can never turn the
    hard stop into a bigger loss than the money model allows.
    """
    per_contract = instrument.option_tick_size * instrument.point_value
    worst = (
        risk.stop_loss
        + execution.stop_order.limit_offset_ticks * per_contract * risk.max_contracts
        + 2 * risk.fee_per_contract_per_side * risk.max_contracts
    )
    if worst > risk.stop_max:
        raise ConfigError(
            f"a stop filled at its limit loses {worst}, over stop_max {risk.stop_max}: "
            "reduce stop_order.limit_offset_ticks or stop_loss"
        )


@dataclass(frozen=True, slots=True)
class Profiles:
    instrument: InstrumentProfile
    risk: RiskProfile
    strategy: StrategyProfile
    execution: ExecutionProfile


def load_profiles(config_dir: Path, instrument_file: str) -> Profiles:
    """The one start-up path: every profile, plus the rules that span profiles."""
    profiles = Profiles(
        instrument=load_instrument(config_dir / "instruments" / instrument_file),
        risk=load_risk(config_dir / "risk.yaml"),
        strategy=load_strategy(config_dir / "strategy.yaml"),
        execution=load_execution(config_dir / "execution.yaml"),
    )
    check_consistency(profiles.instrument, profiles.risk, profiles.execution)
    return profiles
