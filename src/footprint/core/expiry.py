"""When an option stops trading, and whether it is a quarterly (American) one.

Both come from the instrument's `option_expiry` rules (the exchange rulebook, chapter 359A; see
config/instruments/nq.yaml): weekly and end-of-month options stop trading at the
configured time on their expiry date, earlier on the stock market's early-close days;
quarterly options are American and are not valued or traded by Footprint (decision
B76-2). Which options are quarterly goes by the trading class, not the date: on a
quarterly Friday IBKR lists the quarterly class (on the expiring future) beside a
weekly class (on the next future), as IBKR listed them on 2026-10-22.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from footprint.core.config import InstrumentProfile
from footprint.core.options import OptionQuote


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


def is_american(quote: OptionQuote, instrument: InstrumentProfile) -> bool:
    """True when the option's trading class is one of the instrument's quarterly
    (American) classes. Classes are matched exactly, as IBKR sends them."""
    if not isinstance(quote, OptionQuote):
        raise TypeError(f"quote must be an OptionQuote, got {quote!r}")
    return quote.trading_class in instrument.option_expiry.american_trading_classes


def is_european_class(name: str, instrument: InstrumentProfile) -> bool:
    """True only for a class that is not American and whose whole name matches one of
    the instrument's European patterns. Anything else (a class never seen, a renamed
    one) is not European: fail closed."""
    rules = instrument.option_expiry
    if name in rules.american_trading_classes:
        return False
    return any(re.fullmatch(p, name) for p in rules.european_trading_class_patterns)
