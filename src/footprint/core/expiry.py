"""When an option stops trading, and whether its expiry is a quarterly (American) one.

Both come from the instrument's `option_expiry` rules (the exchange rulebook, chapter 359A; see
config/instruments/nq.yaml): weekly and end-of-month options stop trading at the
configured time on their expiry date, earlier on the stock market's early-close days;
quarterly options are American and are not valued or traded by Footprint (decision
B76-2). The quarterly rule goes by date only: a quarterly expiry moved by a holiday is
not recognised.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from footprint.core.config import InstrumentProfile


def expiry_moment(expiry: date, instrument: InstrumentProfile) -> datetime:
    """The UTC moment an option expiring on `expiry` stops trading: the rule's `time`,
    or on one of its early-close dates the earlier of `early_close_time` and `time`,
    both in the rule's own timezone. Nothing trades after the futures halt, so on a day
    in the futures' `early_closes` the moment is never later than that halt (it can only
    move the moment earlier)."""
    if not isinstance(expiry, date) or isinstance(expiry, datetime):
        raise TypeError(f"expiry must be a date, got {expiry!r}")
    rules = instrument.option_expiry
    stop = rules.time
    if expiry in rules.early_close_dates:
        stop = min(stop, rules.early_close_time)
    moment = datetime.combine(expiry, stop, ZoneInfo(rules.timezone)).astimezone(UTC)
    halt = instrument.early_closes.get(expiry)
    if halt is not None:
        zone = ZoneInfo(instrument.prime_session.timezone)
        moment = min(moment, datetime.combine(expiry, halt, zone).astimezone(UTC))
    return moment


def is_american_expiry(expiry: date, instrument: InstrumentProfile) -> bool:
    """True when `expiry` is the rule's weekday, in the rule's week, of a quarterly month.

    Weeks are counted from the first of the month: the first such weekday of the month
    is in week one, whatever weekday the month starts on."""
    if not isinstance(expiry, date) or isinstance(expiry, datetime):
        raise TypeError(f"expiry must be a date, got {expiry!r}")
    rules = instrument.option_expiry
    if expiry.month not in rules.american_months or expiry.weekday() != rules.american_weekday:
        return False
    week = (expiry.day - 1) // _DAYS_PER_WEEK + 1
    return week == rules.american_week_of_month


_DAYS_PER_WEEK = len(calendar.day_name)
