"""A cached conversion of a moment to a time zone's local time.

The readers convert the same bar timestamps to local time on every decision; over a
backtest that is millions of identical conversions. The result depends only on the
moment and the zone name, so caching it cannot change any answer.
"""

from __future__ import annotations

from datetime import datetime
from functools import cache, lru_cache
from zoneinfo import ZoneInfo

# About a year of 1-minute timestamps in one zone; old entries are evicted first.
_CACHE_SIZE = 2_000_000


@cache  # one entry per zone name: a handful
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


@lru_cache(maxsize=_CACHE_SIZE)
def local_time(moment: datetime, timezone: str) -> datetime:
    """`moment` (an aware datetime) as local time in `timezone`."""
    return moment.astimezone(_zone(timezone))
