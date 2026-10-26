"""A deterministic synthetic NQ 1-minute market for the speed work (BH-11).

Many sessions of nearly 23-hour data: every tradable day's overnight (15:00 PT the
calendar day before) through the prime close (13:59 PT), a seeded random walk in
quarter-point ticks around 20,000 with time-of-day volume, some trending days and
volume spikes, so the play caller's 61-session history fills, the crew's 20-session
look-backs fill and the momentum entry can fire. Nothing here is a market model: it
only has to drive every part of the engine, the same way on every run.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from footprint.core.calendar import is_tradable_day
from footprint.core.config import InstrumentProfile
from footprint.core.models import Bar

PT = ZoneInfo("America/Los_Angeles")
D = Decimal
TICK = D("0.25")
START_PRICE = D(20000)
_NIGHT_START = time(15, 0)
_PRIME_END = time(14, 0)


def tradable_days(first: date, count: int, instrument: InstrumentProfile) -> list[date]:
    days: list[date] = []
    day = first
    while len(days) < count:
        if is_tradable_day(day, instrument):
            days.append(day)
        day += timedelta(days=1)
    return days


def speed_market(
    instrument: InstrumentProfile, first: date, sessions: int, seed: int = 11
) -> list[Bar]:
    """`sessions` tradable days of bars from `first` on, the same bars for one seed."""
    rng = random.Random(seed)
    price = START_PRICE
    bars: list[Bar] = []
    for day in tradable_days(first, sessions, instrument):
        start = datetime.combine(day - timedelta(days=1), _NIGHT_START, PT).astimezone(UTC)
        end = datetime.combine(day, _PRIME_END, PT).astimezone(UTC)
        drift = rng.choice((-6, -3, -1, 0, 0, 1, 3, 6))  # ticks a minute on a trending day
        moment = start
        push = 0  # ticks a minute added after a spike (its follow-through)
        push_left = 0
        while moment < end:
            local = moment.astimezone(PT).time()
            cash = time(6, 30) <= local < time(13, 0)
            base = 400 if cash else 80
            volume = base + rng.randrange(base)
            sigma = 6 if cash else 3
            ticks = rng.randint(-sigma, sigma) + (drift if cash else 0)
            if push_left:
                ticks += push
                push_left -= 1
            elif rng.random() < (0.01 if cash else 0.003):  # a volume spike with a push
                volume *= rng.randint(3, 6)
                side = rng.choice((-1, 1))
                ticks += side * rng.randint(10, 24)
                push, push_left = side * rng.randint(2, 5), rng.randint(3, 15)
            o = price
            c = o + TICK * ticks
            # Wide wicks often enough that a resting entry can fill inside its minute.
            high = max(o, c) + TICK * rng.randint(0, 48 if rng.random() < 0.3 else 4)
            low = min(o, c) - TICK * rng.randint(0, 48 if rng.random() < 0.3 else 4)
            bars.append(Bar(instrument.symbol, moment, o, high, low, c, volume))
            price = c
            moment += timedelta(minutes=1)
    return bars
