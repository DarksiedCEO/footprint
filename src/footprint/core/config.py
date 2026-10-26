"""Loads and validates the YAML profiles under config/. Bad values raise ConfigError."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from footprint.core.holiday_rules import (
    KINDS,
    OBSERVED,
    REQUIRED_RULES,
    HolidayRule,
    MissingEaster,
    missing_days,
)
from footprint.core.options import is_trading_class

_PERCENT_MAX = Decimal(100)  # upper bound of a percentage (reviewed in the A-18 guard)


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
class OptionExpiry:
    """When the traded options stop trading, and which expiries are American (the
    exchange rulebook, chapter 359A).

    An option expires at `time` (in `timezone`) on its expiry date; on a date in
    `early_close_dates` (the stock market's early closes, not the futures halts), at the
    earlier of `early_close_time` and `time`. An option whose trading class is in
    `american_trading_classes` is a quarterly (American-style) one, whatever its date: a
    weekly class is listed on the quarterly Fridays too. Only a class matching one of
    `european_trading_class_patterns` (whole name, regular expressions) is valued as
    European; any other class is refused, never assumed European."""

    early_close_time: time  # (before `time`: that field name shadows the type below it)
    time: time
    early_close_dates: frozenset[date]
    timezone: str
    american_trading_classes: frozenset[str]
    european_trading_class_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EarlyClosePeriod:
    """Every trading day from `start` through `through` (inclusive), the prime session
    ends at `close` (prime_session timezone), unless that day closes earlier still."""

    start: date
    through: date
    close: time


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
    option_expiry: OptionExpiry
    trading_days: frozenset[int]
    holidays: frozenset[date]
    # The prime session ends early (prime_session timezone): the listed dates, merged with
    # every trading day of each early_close_period (the earlier close wins).
    early_closes: Mapping[date, time]
    # Spans in which the prime session ends at one time every trading day (an exchange
    # halt inside the prime session, e.g. the exchange's former daily equity maintenance halt;
    # backtest-history-v1, BH-3). Holidays are skipped; already merged into early_closes.
    early_close_periods: tuple[EarlyClosePeriod, ...]
    # Open at the exchange, but Footprint treats them as closed (no session, no entry)
    # until they are reviewed and approved (US-holiday sessions, remediation item 8).
    no_trade_days: frozenset[date]
    # The exchange's standard closures by rule, and Easter Sundays (for rules counted
    # from Easter): a calendar missing one of these days up to calendar_through is
    # refused (core/holiday_rules.py).
    holiday_rules: tuple[HolidayRule, ...]
    easter_sundays: frozenset[date]
    # Holidays and early closes are complete from calendar_from to calendar_through.
    # The start is fixed in config, never taken from the dates listed, so deleting a
    # whole year cannot switch its check off (independent audit A-8).
    calendar_from: date
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


_RULE_KEYS = {
    "name",
    "month",
    "day",
    "observed",
    "weekday",
    "nth",
    "days_from_easter",
    "days_after",
    "kind",
    "first_year",
    "weekdays",
}


def _holiday_rules(raw: Mapping[str, object], key: str, where: str) -> tuple[HolidayRule, ...]:
    value = raw[key]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: {key} must be a non-empty list of rules")
    rules = []
    for item in value:
        if not isinstance(item, dict) or not set(item) <= _RULE_KEYS or "name" not in item:
            raise ConfigError(f"{where}: {key}: each rule needs a name and only {_RULE_KEYS}")
        name = item["name"]
        weekday = item.get("weekday")
        if weekday is not None and weekday not in _WEEKDAYS:
            raise ConfigError(f"{where}: {key} {name}: weekday must be one of {_WEEKDAYS}")
        observed = item.get("observed", "none")
        kind = item.get("kind", "closed")
        if kind not in KINDS:
            raise ConfigError(f"{where}: {key} {name}: kind must be one of {KINDS}")
        if observed not in OBSERVED:
            raise ConfigError(f"{where}: {key} {name}: observed must be one of {OBSERVED}")
        numbers = {
            k: item.get(k) for k in ("month", "day", "nth", "days_from_easter", "days_after")
        }
        if any(
            v is not None and (not isinstance(v, int) or isinstance(v, bool))
            for v in numbers.values()
        ):
            raise ConfigError(
                f"{where}: {key} {name}: month, day, nth, days_from_easter are integers"
            )
        first_year = item.get("first_year")
        if first_year is not None and (
            not isinstance(first_year, int) or isinstance(first_year, bool)
        ):
            raise ConfigError(f"{where}: {key} {name}: first_year must be a year (an integer)")
        rule = HolidayRule(
            name=str(name),
            month=numbers["month"],
            day=numbers["day"],
            observed=observed,
            weekday=None if weekday is None else _WEEKDAYS.index(weekday),
            nth=numbers["nth"],
            days_from_easter=numbers["days_from_easter"],
            days_after=numbers["days_after"] or 0,
            kind=str(kind),
            first_year=first_year,
            weekdays=_rule_weekdays(item, where, f"{key} {name}"),
        )
        by_easter = rule.days_from_easter is not None
        by_weekday = rule.weekday is not None and rule.nth is not None and rule.month is not None
        by_day = rule.day is not None and rule.month is not None
        if [by_easter, by_weekday, by_day].count(True) != 1:
            raise ConfigError(
                f"{where}: {key} {name}: exactly one of month+day, month+weekday+nth, "
                "or days_from_easter"
            )
        if rule.nth is not None and rule.nth not in (-1, 1, 2, 3, 4, 5):
            raise ConfigError(f"{where}: {key} {name}: nth must be 1-5, or -1 for the last")
        rules.append(rule)
    return tuple(rules)


def _rule_weekdays(item: Mapping[str, object], where: str, what: str) -> frozenset[int] | None:
    """A rule's `weekdays`: absent (any weekday), or a non-empty list of distinct days."""
    if "weekdays" not in item:
        return None
    value = item["weekdays"]
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(v, str) or v not in _WEEKDAYS for v in value)
        or len(set(value)) != len(value)
    ):
        raise ConfigError(
            f"{where}: {what}: weekdays must be a non-empty list of distinct days from {_WEEKDAYS}"
        )
    return frozenset(_WEEKDAYS.index(v) for v in value)


_PERIOD_KEYS = {"from", "through", "close"}


def _early_close_periods(
    raw: Mapping[str, object], key: str, where: str
) -> tuple[EarlyClosePeriod, ...]:
    value = raw[key]
    if not isinstance(value, list):
        raise ConfigError(f"{where}: {key} must be a list (empty for none)")
    periods = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _PERIOD_KEYS:
            raise ConfigError(f"{where}: {key}: each period has exactly from, through and close")
        start, through = item["from"], item["through"]
        if any(not isinstance(d, date) or isinstance(d, datetime) for d in (start, through)):
            raise ConfigError(f"{where}: {key}: from and through must be dates (YYYY-MM-DD)")
        if start > through:
            raise ConfigError(f"{where}: {key}: from is after through ({start} > {through})")
        text = item["close"]
        try:
            close = time.fromisoformat(text) if isinstance(text, str) else None
        except ValueError:
            close = None
        if close is None or close.second or close.microsecond or close.tzinfo is not None:
            raise ConfigError(f"{where}: {key} {start}: close must be HH:MM, got {text!r}")
        periods.append(EarlyClosePeriod(start, through, close))
    return tuple(periods)


def _merged_early_closes(
    listed: Mapping[date, time],
    periods: tuple[EarlyClosePeriod, ...],
    trading_days: frozenset[int],
    holidays: frozenset[date],
) -> Mapping[date, time]:
    """The listed early closes plus every trading day of every period (holidays and
    non-trading weekdays skipped); where two apply, the earlier close."""
    closes = dict(listed)
    for period in periods:
        day = period.start
        while day <= period.through:
            if day.weekday() in trading_days and day not in holidays:
                closes[day] = min(closes.get(day, period.close), period.close)
            day += timedelta(days=1)
    return MappingProxyType(closes)


def _clock(body: Mapping[str, object], key: str, where: str) -> time:
    text = _text(body, key, where)
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{where}: {key} must be HH:MM, got {text!r}") from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ConfigError(f"{where}: {key} must be HH:MM with no seconds, got {text!r}")
    return parsed


def _timezone(body: Mapping[str, object], key: str, where: str) -> str:
    zone = _text(body, key, where)
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{where}: unknown timezone {zone!r}") from exc
    return zone


def _option_expiry(raw: Mapping[str, object], key: str, where: str) -> OptionExpiry:
    where = f"{where}: {key}"
    body = _check_keys(raw[key], {f.name for f in fields(OptionExpiry)}, where)
    classes = body["american_trading_classes"]
    if not isinstance(classes, list) or not classes:
        raise ConfigError(
            f"{where}: american_trading_classes must be a non-empty list of trading classes"
        )
    for name in classes:
        if not is_trading_class(name):
            raise ConfigError(
                f"{where}: american_trading_classes entries must be non-empty names with no "
                f"spaces, got {name!r}"
            )
    if len(set(classes)) != len(classes):
        raise ConfigError(f"{where}: american_trading_classes has duplicates")
    patterns = body["european_trading_class_patterns"]
    if not isinstance(patterns, list) or not patterns:
        raise ConfigError(
            f"{where}: european_trading_class_patterns must be a non-empty list of patterns"
        )
    for pattern in patterns:
        if not is_trading_class(pattern):
            raise ConfigError(
                f"{where}: european_trading_class_patterns entries must be non-empty "
                f"patterns with no spaces, got {pattern!r}"
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"{where}: european_trading_class_patterns {pattern!r} is not a valid "
                f"pattern: {exc}"
            ) from exc
        for name in classes:
            if compiled.fullmatch(name):
                raise ConfigError(
                    f"{where}: european_trading_class_patterns {pattern!r} matches the "
                    f"American class {name!r}"
                )
    if len(set(patterns)) != len(patterns):
        raise ConfigError(f"{where}: european_trading_class_patterns has duplicates")
    normal = _clock(body, "time", where)
    early = _clock(body, "early_close_time", where)
    if early >= normal:
        raise ConfigError(f"{where}: early_close_time must be before time")
    return OptionExpiry(
        time=normal,
        early_close_time=early,
        early_close_dates=_holidays(body, "early_close_dates", where),
        timezone=_timezone(body, "timezone", where),
        american_trading_classes=frozenset(classes),
        european_trading_class_patterns=tuple(patterns),
    )


def _date(raw: Mapping[str, object], key: str, where: str) -> date:
    value = raw[key]
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ConfigError(f"{where}: {key} must be a date (YYYY-MM-DD), got {value!r}")
    return value


def load_instrument(path: Path) -> InstrumentProfile:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(InstrumentProfile)})
    trading_days = _trading_days(raw, "trading_days", where)
    holidays = _holidays(raw, "holidays", where)
    periods = _early_close_periods(raw, "early_close_periods", where)
    listed_early = _early_closes(raw, "early_closes", where)
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
        option_expiry=_option_expiry(raw, "option_expiry", where),
        trading_days=trading_days,
        holidays=holidays,
        early_closes=_merged_early_closes(listed_early, periods, trading_days, holidays),
        early_close_periods=periods,
        no_trade_days=_holidays(raw, "no_trade_days", where),
        holiday_rules=_holiday_rules(raw, "holiday_rules", where),
        easter_sundays=_holidays(raw, "easter_sundays", where),
        calendar_from=_date(raw, "calendar_from", where),
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
    listed = {
        "holidays": profile.holidays,
        "early_closes": profile.early_closes,
        "no_trade_days": profile.no_trade_days,
        "option_expiry early_close_dates": profile.option_expiry.early_close_dates,
    }
    for source, days in listed.items():
        for day in days:
            if day.weekday() not in profile.trading_days:
                raise ConfigError(
                    f"{where}: {source} {day} is a {day:%A}, not a trading day: a holiday "
                    "or early close goes on the weekday the exchange observes it (e.g. "
                    "Friday for a Saturday)"
                )
    for day, close in profile.early_closes.items():
        if day in profile.holidays:
            raise ConfigError(f"{where}: {day} is both a holiday and an early close")
        if not (cash.start < close < prime.end):
            raise ConfigError(
                f"{where}: early close {day} {close} must be after the cash open and "
                "before the normal prime close"
            )
    for day in profile.no_trade_days:
        if day in profile.holidays:
            raise ConfigError(f"{where}: {day} is both a holiday and a no-trade day")
    dated = (*profile.holidays, *profile.early_closes, *profile.no_trade_days)
    if profile.calendar_from > profile.calendar_through:
        raise ConfigError(f"{where}: calendar_from is after calendar_through")
    late = [d for d in dated if d > profile.calendar_through]
    if late:
        raise ConfigError(f"{where}: {sorted(late)[0]} is after calendar_through")
    early = [d for d in dated if d < profile.calendar_from]
    if early:
        raise ConfigError(f"{where}: {sorted(early)[0]} is before calendar_from")
    for period in profile.early_close_periods:
        if period.start < profile.calendar_from:
            raise ConfigError(
                f"{where}: early_close_periods {period.start} is before calendar_from"
            )
        if period.through > profile.calendar_through:
            raise ConfigError(
                f"{where}: early_close_periods {period.through} is after calendar_through"
            )
    # Complete from calendar_from through calendar_through: every standard US exchange
    # holiday in that window must be listed (closed, early close or no-trade day), so
    # moving calendar_through forward over a standard closure alone is refused (D34).
    names = [r.name for r in profile.holiday_rules]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:  # AEGIS I-3: a second rule of the same name could mask the first
        raise ConfigError(f"{where}: holiday_rules has more than one rule named {repeated}")
    kinds = {r.name: r.kind for r in profile.holiday_rules}
    absent = [n for n in REQUIRED_RULES if n not in kinds]
    if absent:
        raise ConfigError(f"{where}: holiday_rules has no rule for {', '.join(absent)}")
    wrong = [f"{n} must be {k}" for n, k in REQUIRED_RULES.items() if kinds[n] != k]
    if wrong:
        raise ConfigError(f"{where}: holiday_rules: {'; '.join(wrong)}")
    for easter in profile.easter_sundays:
        if easter.weekday() != _WEEKDAYS.index("SUN"):
            raise ConfigError(f"{where}: easter_sundays {easter} is not a Sunday")
    try:
        missing = missing_days(
            profile.holiday_rules,
            profile.easter_sundays,
            {*profile.holidays, *profile.no_trade_days},
            # only the LISTED early closes: a period's routine halt (BH-3) is not the
            # exchange's early close for a rule's day, and must not hide a missing one
            set(listed_early),
            profile.calendar_from,
            profile.calendar_through,
        )
    except MissingEaster as exc:
        raise ConfigError(f"{where}: {exc}") from None
    except ValueError as exc:  # a rule naming a day that does not exist
        raise ConfigError(f"{where}: holiday_rules: {exc}") from None
    if missing:
        day, name = missing[0]
        raise ConfigError(
            f"{where}: calendar_through {profile.calendar_through} but {day} ({name}) is not "
            f"listed ({len(missing)} standard holiday(s) missing): list the exchange's "
            "holidays and early closes up to calendar_through"
        )
    _check_option_early_closes(profile, where)
    return profile


def _check_option_early_closes(profile: InstrumentProfile, where: str) -> None:
    """The options' early-close dates (the stock market's half days) are held to the
    same completeness rule as the futures calendar: inside calendar_from..through, and
    carrying every early-close rule's day in that window that is not an exchange holiday.
    A no-trade day does not excuse one: it is Footprint's own choice, and the stock
    market (and so the options' last trading time) still closes early that day."""
    rules = profile.option_expiry
    for day in sorted(rules.early_close_dates):
        if not profile.calendar_from <= day <= profile.calendar_through:
            raise ConfigError(
                f"{where}: option_expiry: early_close_dates {day} is outside "
                f"calendar_from {profile.calendar_from} to calendar_through "
                f"{profile.calendar_through}"
            )
    _, early_kind = KINDS  # holiday_rules.KINDS: (closed, early close)
    early_rules = [rule for rule in profile.holiday_rules if rule.kind == early_kind]
    missing = missing_days(
        early_rules,
        profile.easter_sundays,
        set(profile.holidays),  # no_trade_days do not count: the stock market still opens
        set(rules.early_close_dates),
        profile.calendar_from,
        profile.calendar_through,
    )
    if missing:
        day, name = missing[0]
        raise ConfigError(
            f"{where}: option_expiry: early_close_dates lacks {day} ({name}): list the "
            "stock market's early closes up to calendar_through"
        )


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
class DecayExitSettings:
    """Decision DX-1: whether the decay exit runs, its window and its share of the open
    profit."""

    enabled: bool
    window_minutes: int
    max_pct_of_open_profit: Decimal


@dataclass(frozen=True, slots=True)
class ThetaSettings:
    planned_hold_minutes: int
    max_decay_pct_of_target: Decimal
    rate: Decimal
    min_implied_vol: Decimal
    max_implied_vol: Decimal
    decay_exit: DecayExitSettings


@dataclass(frozen=True, slots=True)
class VegaSettings:
    min_implied_vol: Decimal
    max_implied_vol: Decimal


@dataclass(frozen=True, slots=True)
class DeltaSettings:
    min_abs_delta: Decimal
    max_abs_delta: Decimal


@dataclass(frozen=True, slots=True)
class GammaSettings:
    min_hours_to_expiry: Decimal


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
    theta: ThetaSettings
    vega: VegaSettings
    delta: DeltaSettings
    gamma: GammaSettings
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

    th_where = f"{where}: theta"
    th = _check_keys(raw["theta"], {f.name for f in fields(ThetaSettings)}, th_where)
    rate = th["rate"]
    if isinstance(rate, bool) or not isinstance(rate, int | float):
        raise ConfigError(f"{th_where}: rate must be a number, got {rate!r}")
    dx_where = f"{th_where}: decay_exit"
    dx = _check_keys(th["decay_exit"], {f.name for f in fields(DecayExitSettings)}, dx_where)
    decay_exit = DecayExitSettings(
        enabled=_boolean(dx, "enabled", dx_where),
        window_minutes=_integer(dx, "window_minutes", dx_where, minimum=1),
        max_pct_of_open_profit=_positive_decimal(dx, "max_pct_of_open_profit", dx_where),
    )
    if decay_exit.max_pct_of_open_profit > _PERCENT_MAX:
        raise ConfigError(f"{dx_where}: max_pct_of_open_profit must be <= {_PERCENT_MAX}")
    theta = ThetaSettings(
        planned_hold_minutes=_integer(th, "planned_hold_minutes", th_where, minimum=1),
        max_decay_pct_of_target=_positive_decimal(th, "max_decay_pct_of_target", th_where),
        rate=Decimal(str(rate)),
        min_implied_vol=_positive_decimal(th, "min_implied_vol", th_where),
        max_implied_vol=_positive_decimal(th, "max_implied_vol", th_where),
        decay_exit=decay_exit,
    )
    if not theta.rate.is_finite():
        raise ConfigError(f"{th_where}: rate must be finite, got {rate!r}")
    if theta.max_decay_pct_of_target > _PERCENT_MAX:
        raise ConfigError(f"{th_where}: max_decay_pct_of_target must be <= {_PERCENT_MAX}")
    if theta.max_implied_vol <= theta.min_implied_vol:
        raise ConfigError(f"{th_where}: max_implied_vol must be > min_implied_vol")

    # GV-1: the vega band is also the bracket every Greeks agent solves implied
    # volatility in. It must lie inside theta's bounds: a vega bound outside them could
    # never be reached (Theta refuses first), a setting that would only mislead.
    vg_where = f"{where}: vega"
    vg = _check_keys(raw["vega"], {f.name for f in fields(VegaSettings)}, vg_where)
    vega = VegaSettings(
        min_implied_vol=_positive_decimal(vg, "min_implied_vol", vg_where),
        max_implied_vol=_positive_decimal(vg, "max_implied_vol", vg_where),
    )
    if vega.max_implied_vol <= vega.min_implied_vol:
        raise ConfigError(f"{vg_where}: max_implied_vol must be > min_implied_vol")
    if vega.min_implied_vol < theta.min_implied_vol or vega.max_implied_vol > theta.max_implied_vol:
        raise ConfigError(
            f"{vg_where}: the band must lie inside theta's [min_implied_vol, max_implied_vol]"
        )

    dl_where = f"{where}: delta"
    dl = _check_keys(raw["delta"], {f.name for f in fields(DeltaSettings)}, dl_where)
    delta = DeltaSettings(
        min_abs_delta=_positive_decimal(dl, "min_abs_delta", dl_where),
        max_abs_delta=_positive_decimal(dl, "max_abs_delta", dl_where),
    )
    if delta.max_abs_delta <= delta.min_abs_delta:
        raise ConfigError(f"{dl_where}: max_abs_delta must be > min_abs_delta")
    if delta.max_abs_delta > 1:
        raise ConfigError(f"{dl_where}: max_abs_delta must be <= 1")
    # Review M1, interim until Andre decides GV-5: N (the levels room check) assumes
    # options.assumed_delta, which is pessimistic only while Delta lets no entry through
    # with a smaller delta.
    if delta.min_abs_delta < options.assumed_delta:
        raise ConfigError(
            f"{dl_where}: min_abs_delta {delta.min_abs_delta} must be >= "
            f"options.assumed_delta {options.assumed_delta}: below it, N would understate "
            "the move an allowed entry needs (GV-5)"
        )

    gm_where = f"{where}: gamma"
    gm = _check_keys(raw["gamma"], {f.name for f in fields(GammaSettings)}, gm_where)
    gamma = GammaSettings(
        min_hours_to_expiry=_positive_decimal(gm, "min_hours_to_expiry", gm_where),
    )

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
        theta=theta,
        vega=vega,
        delta=delta,
        gamma=gamma,
        zeroday=zeroday,
        console=console,
    )


@dataclass(frozen=True, slots=True)
class RegimeSettings:
    """The play caller's regime reader (config/regime.yaml)."""

    decision_minutes: int
    efficiency_lookback_sessions: int
    trend_rank_pct: Decimal
    chop_rank_pct: Decimal
    overnight_lookback_sessions: int


def load_regime(path: Path) -> RegimeSettings:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(RegimeSettings)})
    settings = RegimeSettings(
        decision_minutes=_integer(raw, "decision_minutes", where, minimum=1),
        efficiency_lookback_sessions=_integer(
            raw, "efficiency_lookback_sessions", where, minimum=1
        ),
        trend_rank_pct=_positive_decimal(raw, "trend_rank_pct", where),
        chop_rank_pct=_positive_decimal(raw, "chop_rank_pct", where),
        overnight_lookback_sessions=_integer(raw, "overnight_lookback_sessions", where, minimum=1),
    )
    if settings.trend_rank_pct > _PERCENT_MAX:
        raise ConfigError(f"{where}: trend_rank_pct must be <= {_PERCENT_MAX}")
    if settings.chop_rank_pct >= settings.trend_rank_pct:
        raise ConfigError(f"{where}: chop_rank_pct must be < trend_rank_pct")
    return settings


@dataclass(frozen=True, slots=True)
class PlaySettings:
    """The play caller (config/play.yaml): which play runs on the day's label (PC-5)."""

    on_time_seconds: int


def load_play(path: Path) -> PlaySettings:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(PlaySettings)})
    return PlaySettings(on_time_seconds=_integer(raw, "on_time_seconds", where, minimum=1))


@dataclass(frozen=True, slots=True)
class DailyEvent:
    """A scheduled moment every calendar day (a release time, the cash open), on the
    news rules' clock (NewsRulesSettings.timezone)."""

    name: str
    at: time
    time_stop: bool  # N-OPEN-2: an entry just after this window gets the news time stop


@dataclass(frozen=True, slots=True)
class NewsRulesSettings:
    """The scheduled news and opening-bell windows (config/news.yaml; Andre's N1-N4 and
    the N-OPEN defaults, decisions-pending.md). Every window is a blackout: no new
    entries, and a held position is closed (decision/news_rules.py)."""

    timezone: str
    lead_minutes: int  # N2: a daily window starts this long before its event
    lag_minutes: int  # N2: ... and ends this long after it (its whole end minute)
    daily_events: tuple[DailyEvent, ...]  # N1, N3 (each with its N-OPEN-2 switch)
    pre_news_until: str  # N4 / N-OPEN-1: the daily event whose window ends it
    pre_news_time_stop: bool  # N-OPEN-2 for the N4 window
    overnight_flat_from: time  # N-OPEN-3, on the overnight session's clock


def _daily_events(raw: Mapping[str, object], key: str, where: str) -> tuple[DailyEvent, ...]:
    items = raw[key]
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{where}: {key} must be a non-empty list")
    events: list[DailyEvent] = []
    for item in items:
        inner = f"{where}: {key}"
        body = _check_keys(item, {f.name for f in fields(DailyEvent)}, inner)
        event = DailyEvent(
            _text(body, "name", inner),
            _clock(body, "at", where),
            _boolean(body, "time_stop", inner),
        )
        if any(e.name == event.name for e in events):
            raise ConfigError(f"{where}: {key}: {event.name!r} is listed twice")
        events.append(event)
    return tuple(events)


def load_news_rules(path: Path) -> NewsRulesSettings:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(NewsRulesSettings)})
    events = _daily_events(raw, "daily_events", where)
    until = _text(raw, "pre_news_until", where)
    if until not in {e.name for e in events}:
        raise ConfigError(f"{where}: pre_news_until {until!r} is not one of the daily_events")
    lead = _integer(raw, "lead_minutes", where, minimum=0)
    lag = _integer(raw, "lag_minutes", where, minimum=0)
    # The rules look for windows on the day before, of and after a moment only: a window
    # of a day or more could hide the one the news time stop must look back to.
    if timedelta(minutes=lead + lag) >= timedelta(days=1):
        raise ConfigError(
            f"{where}: lead_minutes + lag_minutes ({lead} + {lag}) must be shorter than one day"
        )
    return NewsRulesSettings(
        timezone=_timezone(raw, "timezone", where),
        lead_minutes=lead,
        lag_minutes=lag,
        daily_events=events,
        pre_news_until=until,
        pre_news_time_stop=_boolean(raw, "pre_news_time_stop", where),
        overnight_flat_from=_clock(raw, "overnight_flat_from", where),
    )


@dataclass(frozen=True, slots=True)
class QuoteSettings:
    max_age_seconds: int
    greeks_max_age_seconds: int


@dataclass(frozen=True, slots=True)
class ContractSettings:
    max_spread_pct_of_mid: Decimal
    max_spread_points: Decimal
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
    fill_price_wait_seconds: int


@dataclass(frozen=True, slots=True)
class IbkrConfig:
    ack_timeout_seconds: int
    positions_disagree_alarm_seconds: int
    resync_realarm_seconds: int
    reconnect_seconds: int
    option_security_type: str
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
    probe_wait_seconds: int
    probe_option_quotes: int
    export_wait_seconds: int
    export_max_missing_minutes: int
    record_interval_seconds: int
    record_minutes: int


@dataclass(frozen=True, slots=True)
class AlarmSettings:
    retry_seconds: int
    max_pending: int
    channel_timeout_seconds: int
    call_cooldown_seconds: int
    degraded_attendance_minutes: int


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
    quotes = QuoteSettings(
        max_age_seconds=_integer(q, "max_age_seconds", q_where, minimum=1),
        greeks_max_age_seconds=_integer(q, "greeks_max_age_seconds", q_where, minimum=1),
    )

    c_where = f"{where}: contract"
    c = _check_keys(raw["contract"], {f.name for f in fields(ContractSettings)}, c_where)
    contract = ContractSettings(
        max_spread_pct_of_mid=_positive_decimal(c, "max_spread_pct_of_mid", c_where),
        max_spread_points=_positive_decimal(c, "max_spread_points", c_where),
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
        fill_price_wait_seconds=_integer(rc, "fill_price_wait_seconds", rc_where, minimum=1),
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
        degraded_attendance_minutes=_integer(al, "degraded_attendance_minutes", a_where, minimum=1),
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
        probe_wait_seconds=_integer(m, "probe_wait_seconds", m_where, minimum=1),
        probe_option_quotes=_integer(m, "probe_option_quotes", m_where, minimum=1),
        export_wait_seconds=_integer(m, "export_wait_seconds", m_where, minimum=1),
        export_max_missing_minutes=_integer(m, "export_max_missing_minutes", m_where, minimum=0),
        record_interval_seconds=_integer(m, "record_interval_seconds", m_where, minimum=1),
        record_minutes=_integer(m, "record_minutes", m_where, minimum=1),
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

    Felix's absolute spread cap (contract.max_spread_points) must be at least one
    option tick: a smaller one refuses every quote that is not locked.
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
    # M3 (b), review F3: a spread cap under one option tick would refuse every quote
    # that is not locked, silently stopping all entries.
    if execution.contract.max_spread_points < instrument.option_tick_size:
        raise ConfigError(
            f"contract.max_spread_points {execution.contract.max_spread_points} is under "
            f"one option tick ({instrument.option_tick_size}): every quote would be refused"
        )


@dataclass(frozen=True, slots=True)
class Profiles:
    instrument: InstrumentProfile
    risk: RiskProfile
    strategy: StrategyProfile
    execution: ExecutionProfile
    regime: RegimeSettings
    play: PlaySettings
    news_rules: NewsRulesSettings


def load_profiles(config_dir: Path, instrument_file: str) -> Profiles:
    """The one start-up path: every profile, plus the rules that span profiles."""
    profiles = Profiles(
        instrument=load_instrument(config_dir / "instruments" / instrument_file),
        risk=load_risk(config_dir / "risk.yaml"),
        strategy=load_strategy(config_dir / "strategy.yaml"),
        execution=load_execution(config_dir / "execution.yaml"),
        regime=load_regime(config_dir / "regime.yaml"),
        play=load_play(config_dir / "play.yaml"),
        news_rules=load_news_rules(config_dir / "news.yaml"),
    )
    check_consistency(profiles.instrument, profiles.risk, profiles.execution)
    _check_gamma_reachable(
        profiles.strategy.gamma, profiles.instrument, config_dir / "strategy.yaml"
    )
    check_play_timing(profiles.instrument, profiles.execution, profiles.regime, profiles.play)
    return profiles


def _check_gamma_reachable(gamma: GammaSettings, instrument: InstrumentProfile, path: Path) -> None:
    """The longest any entry can wait for its expiry: entries are made only in the prime
    session, and Felix never takes an option expiring more than dte_max calendar days
    after the trading day; an option stops trading at option_expiry.time (early closes
    only make that earlier). So the longest wait is from a prime-session start to the
    expiry time dte_max days later, measured on real clocks over the maintained calendar
    (a clock change can add an hour). A minimum above it refuses every entry, and a huge
    one cannot even be held as a time span (review L1, R2-4)."""
    prime = ZoneInfo(instrument.prime_session.timezone)
    expiry = ZoneInfo(instrument.option_expiry.timezone)
    span = timedelta(days=instrument.dte_max)
    longest = timedelta(0)
    day = instrument.calendar_from
    while day <= instrument.calendar_through:
        opens = datetime.combine(day, instrument.prime_session.start, prime).astimezone(UTC)
        ends = datetime.combine(day + span, instrument.option_expiry.time, expiry).astimezone(UTC)
        longest = max(longest, ends - opens)
        day += timedelta(days=1)
    hours = longest / timedelta(hours=1)
    if gamma.min_hours_to_expiry > Decimal(str(hours)):
        raise ConfigError(
            f"{path}: gamma: min_hours_to_expiry {gamma.min_hours_to_expiry} is over "
            f"{hours:g} hours, the longest any entry can wait for its expiry (prime "
            f"session start to option_expiry.time, dte_max {instrument.dte_max} days "
            "later): no option Felix may pick could meet it"
        )


def check_play_timing(
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
    regime: RegimeSettings,
    play: PlaySettings,
) -> None:
    """The play caller's timing rules that span profiles. Raises ConfigError.

    - The day's label is made `regime.decision_minutes` after the cash open, so that
      moment must fall inside the cash session (the regime reader's own check, made at
      load so a bad config is refused at start-up).
    - The opening's final minute bar is polled `bar_poll_delay_seconds` after it closes;
      at or past `play.on_time_seconds` it could never arrive on time and every live day
      would stand aside (review round 1, L1).
    """
    cash = instrument.cash_session
    start = datetime.combine(date.min, cash.start)
    cash_minutes = (datetime.combine(date.min, cash.end) - start) // timedelta(minutes=1)
    if cash.end <= cash.start or regime.decision_minutes >= cash_minutes:
        raise ConfigError(
            f"regime.decision_minutes ({regime.decision_minutes}) must end inside the cash "
            f"session ({cash.start} to {cash.end})"
        )
    delay = execution.market_data.bar_poll_delay_seconds
    if delay >= play.on_time_seconds:
        raise ConfigError(
            f"market_data.bar_poll_delay_seconds ({delay}) must be below "
            f"play.on_time_seconds ({play.on_time_seconds}): the opening's final minute "
            "could never be read on time"
        )
