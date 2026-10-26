"""An extract of tournament.py (`claude/research/tournament.py` in the Claude project,
sha256 4c0db0b7dcea5bb52fa2f5d33a9a9905ba29b894ac9c8f50404b536154a3069c): the parts that make the
noise-band signals, copied as written, so the tests can compare the harness rule with
the source rule on synthetic data. Changes from the source: `load` takes rows instead
of a path (the CSV reading is unchanged otherwise), the unused VWAP and exit code is left
out, and `first_signals` is the source's `run` loop stopped at the first signal of each
day (the pre-registered first-signal-per-day). Test-only; never imported by src/.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from datetime import datetime, time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
WARMUP_DAYS = 14
MAX_TRADES_PER_DAY = 8
FLAT_MINUTE = 385  # 15:55: flat before the close


class Day:
    __slots__ = ("adr", "c", "date", "h", "l", "m", "o", "prev_close", "sigma")

    def __init__(self, date: object) -> None:
        self.date = date
        self.o: list[float] = []
        self.h: list[float] = []
        self.l: list[float] = []
        self.c: list[float] = []
        self.m: list[int] = []  # minute of session, 0 = 09:30
        self.prev_close = math.nan
        self.adr = math.nan
        self.sigma: list[float] = []


def load(rows: Iterable[Mapping[str, str]]) -> list[Day]:
    days: dict[object, Day] = {}
    order: list[object] = []
    for r in rows:
        t = datetime.fromisoformat(r["ts"]).astimezone(NY)
        if not (time(9, 30) <= t.time() < time(16, 0)) or t.weekday() > 4:
            continue
        d = t.date()
        if d not in days:
            days[d] = Day(d)
            order.append(d)
        day = days[d]
        day.o.append(float(r["open"]))
        day.h.append(float(r["high"]))
        day.l.append(float(r["low"]))
        day.c.append(float(r["close"]))
        day.m.append((t.hour * 60 + t.minute) - (9 * 60 + 30))
    out = [days[d] for d in sorted(order) if len(days[d].c) >= 300]  # type: ignore[type-var]
    for i, day in enumerate(out):
        if i:
            day.prev_close = out[i - 1].c[-1]
        past = out[max(0, i - WARMUP_DAYS) : i]
        if len(past) == WARMUP_DAYS:
            day.adr = statistics.fmean(max(p.h) - min(p.l) for p in past)
            prof = []
            for k in range(len(day.c)):
                mm = day.m[k]
                vals = []
                for p in past:
                    j = bisect_right(p.m, mm) - 1
                    if j >= 0:
                        vals.append(abs(p.c[j] / p.o[0] - 1))
                prof.append(statistics.fmean(vals) if vals else math.nan)
            day.sigma = prof
    return out


def noise_band_signal(day: Day, mult: float, every: int):  # type: ignore[no-untyped-def]
    top = max(day.o[0], day.prev_close)
    bot = min(day.o[0], day.prev_close)

    def bands(k: int) -> tuple[float, float]:
        s = day.sigma[k] * mult
        return top * (1 + s), bot * (1 - s)

    def signal(k: int) -> int:
        if day.m[k] < 30 or (day.m[k] + 1) % every:
            return 0
        up, lo = bands(k)
        return 1 if day.c[k] > up else -1 if day.c[k] < lo else 0

    return signal


def first_signals(
    days: list[Day], mult: float = 1.5, every: int = 15
) -> list[tuple[object, int, int]]:
    """(date, direction, entry minute) of the first trade of each day, as `run` takes it."""
    out: list[tuple[object, int, int]] = []
    for day in days:
        if math.isnan(day.adr) or math.isnan(day.prev_close):
            continue
        signal = noise_band_signal(day, mult, every)
        n = len(day.c)
        k = 0
        while k < n - 1:
            if day.m[k] >= FLAT_MINUTE:
                break
            d = signal(k)
            if not d:
                k += 1
                continue
            out.append((day.date, d, day.m[k + 1]))
            break
    return out
