"""Where the futures price is inside a 1-minute bar.

A bar gives only open, high, low and close. The path between them is modelled as
open -> first extreme -> second extreme -> close, straight lines in between. The
order of the extremes is chosen against the trade: holding calls (long), the low
comes first; holding puts (short), the high comes first. A stop is therefore hit
before a target in any bar that reaches both. With nothing open, a rising bar goes
low-first and a falling bar high-first.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

from footprint.core.models import Bar, Direction

_MINUTE = int(timedelta(minutes=1).total_seconds())
# Seconds into the minute at which the path reaches each vertex.
_FIRST, _SECOND, _CLOSE = _MINUTE // 3, 2 * _MINUTE // 3, _MINUTE - 5


def path_price(bar: Bar, second: int, against: Direction | None) -> Decimal:
    """The modelled futures price `second` seconds into `bar` (0 = the open)."""
    if not 0 <= second < _MINUTE:
        raise ValueError("second must be within the minute")
    if against is Direction.LONG:
        first, then = bar.low, bar.high
    elif against is Direction.SHORT:
        first, then = bar.high, bar.low
    elif bar.close >= bar.open:
        first, then = bar.low, bar.high
    else:
        first, then = bar.high, bar.low
    points = ((0, bar.open), (_FIRST, first), (_SECOND, then), (_CLOSE, bar.close))
    if second >= _CLOSE:
        return bar.close
    for (t0, p0), (t1, p1) in pairwise(points):
        if t0 <= second <= t1:
            return p0 + (p1 - p0) * Decimal(second - t0) / Decimal(t1 - t0)
    return bar.close
