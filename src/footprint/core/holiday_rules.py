"""The exchange's standard closures by rule, as a completeness check on the calendar in
instruments/*.yaml (remediation of AEGIS re-audit M-3).

The rules are instrument data (`holiday_rules`, `easter_sundays` in the YAML), not code
constants. The lists (`holidays`, `early_closes`, `no_trade_days`) stay the source of
truth for what the exchange does on each day; this only refuses a calendar that claims
to be complete through `calendar_through` while a rule's day is missing from them.
Moving `calendar_through` forward without adding the next year's days is refused.

A rule is one of:
    fixed month and day, `observed`: weekend (Saturday -> Friday, Sunday -> Monday),
        not_saturday (Sunday -> Monday; a Saturday is not observed), or none
    the nth (or, with -1, the last) weekday of a month
    a number of days from Easter Sunday (Easter dates are listed, not computed)
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

_SATURDAY, _SUNDAY = calendar.SATURDAY, calendar.SUNDAY

# The US exchange closures a calendar must carry a rule for (AEGIS LOW-4: the rules are
# config, so deleting one would otherwise switch its check off).
REQUIRED_RULES = (
    "New Year's Day",
    "Martin Luther King Jr. Day",
    "Presidents Day",
    "Good Friday",
    "Memorial Day",
    "Juneteenth",
    "Independence Day",
    "Labor Day",
    "Thanksgiving",
    "Day after Thanksgiving",
    "Christmas Eve",
    "Christmas Day",
)
# weekend: Saturday -> Friday, Sunday -> Monday; not_saturday: Sunday -> Monday, a
# Saturday not observed; weekday_only: not observed on a weekend; none: as is.
OBSERVED = ("weekend", "not_saturday", "weekday_only", "none")
# closed: must be in holidays or no_trade_days; early_close: in early_closes (or closed
# entirely, when it coincides with a closure).
KINDS = ("closed", "early_close")


@dataclass(frozen=True, slots=True)
class HolidayRule:
    name: str
    month: int | None = None
    day: int | None = None
    observed: str = "none"
    weekday: int | None = None
    nth: int | None = None  # 1..5, or -1 for the last
    days_from_easter: int | None = None
    days_after: int = 0  # moved this many days after the rule's date (day after X)
    kind: str = "closed"


class MissingEaster(ValueError):
    """A rule needs Easter for a year that `easter_sundays` does not list."""


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    days = [
        date(year, month, d)
        for d in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, d).weekday() == weekday
    ]
    if nth == -1:
        return days[-1]
    return days[nth - 1] if 0 < nth <= len(days) else None


def _observed(day: date, how: str) -> date | None:
    """The weekday the exchange observes `day` on, or None when not observed."""
    if how == "none":
        return day
    if how == "weekday_only":
        return None if day.weekday() in (_SATURDAY, _SUNDAY) else day
    if day.weekday() == _SATURDAY:
        return day - timedelta(days=1) if how == "weekend" else None
    if day.weekday() == _SUNDAY:
        return day + timedelta(days=1)
    return day


def rule_date(rule: HolidayRule, year: int, easters: Mapping[int, date]) -> date | None:
    """The rule's day in `year` (None when not observed that year)."""
    if rule.days_from_easter is not None:
        if year not in easters:
            raise MissingEaster(f"easter_sundays has no date in {year} (needed by {rule.name})")
        return easters[year] + timedelta(days=rule.days_from_easter)
    if rule.month is None:
        raise ValueError(f"holiday rule {rule.name!r} has no month")
    if rule.weekday is not None and rule.nth is not None:
        base = _nth_weekday(year, rule.month, rule.weekday, rule.nth)
        return None if base is None else base + timedelta(days=rule.days_after)
    if rule.day is None:
        raise ValueError(f"holiday rule {rule.name!r} has neither a day nor a weekday")
    return _observed(date(year, rule.month, rule.day), rule.observed)


def missing_days(
    rules: Iterable[HolidayRule],
    easters: Iterable[date],
    closed: set[date],
    early: set[date],
    start: date,
    through: date,
) -> list[tuple[date, str]]:
    """Every rule's day in [start, through] not listed as it must be: a closure in
    `closed` (holidays, no-trade days), an early close in `early` or `closed`."""
    by_year = {d.year: d for d in easters}
    found = []
    for year in range(start.year, through.year + 1):
        for rule in rules:
            day = rule_date(rule, year, by_year)
            if day is None or not start <= day <= through:
                continue
            allowed = closed if rule.kind == "closed" else closed | early
            if day not in allowed:
                found.append(
                    (day, rule.name if rule.kind == "closed" else f"{rule.name}, early close")
                )
    return sorted(found)
