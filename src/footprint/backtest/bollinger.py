"""Bollinger Band entry rules (backtest rule library only).

Andre's approved definitions (2026-10-17) are in docs/strategy-rules.md. Bands: a simple
moving average of the close over `period` bars (the middle line) plus and minus `width`
population standard deviations of those closes (the upper and lower bands). Every
window and count below comes from config/backtest.yaml `rules: bollinger:`.

How each word is read here (for Andre to confirm):

- Bars are completed bars of the rule's timeframe (`fast_minutes` or `slow_minutes`),
  built from the one-minute bars handed in. A rule is asked once per completed bar of
  its timeframe: on any other minute it answers NEUTRAL, so one bar never fires twice.
- Bands run over consecutive bars of the timeframe, across session breaks, as a chart
  draws them.
- A "touch" counts at or beyond the line; a "close outside" is strictly beyond the band.
- Squeeze breakout: a squeeze bar is one whose bandwidth ((upper - lower) / middle) is
  the lowest of the last `squeeze_lookback` bars, itself included. Long: within
  `squeeze_window` bars after a squeeze bar, the first bar that closes above the upper
  band. Short: the mirror (close below the lower band).
- Band-walk pullback: a touch bar is one whose low reaches the middle line after at
  least `walk_min_closes` closes at or above the upper band in the `walk_lookback` bars
  before it. Long: the first later bar, within `walk_window` bars of the touch, that
  closes above the middle line and above the prior bar's high. A close below the lower
  band on any bar after the last walk close cancels it (such a bar is a break, not a
  pullback), so a later bar cannot start a fresh pullback after the break. A touch bar
  may lie wholly below the middle line. Short: the mirror.
- Band bounce: long when the middle line is higher than `slope_bars` bars ago, a bar
  within the last `bounce_window` bars touched the lower band, and this bar is the first
  since that touch to close back inside the bands on an up candle (close above open).
  The slope is measured at the signal bar. Short: the mirror (falling middle, upper
  band, down candle).
- Andre's readings (2026-10-17): in the walk, a close beyond the far band inside the
  lookback resets the count, so only walk closes after the last break count; in the walk
  and the bounce, a bar that itself fired cannot be the touch for a second entry (one
  setup, one entry). A touch bar that looks like a confirm but did not fire still arms.

Pure and stateless: the same bars give the same answer however often it is asked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from footprint.core.models import Bar, Read, Score
from footprint.core.series import require_series
from footprint.data.aggregate import resample


@dataclass(frozen=True, slots=True)
class BollingerSettings:
    """The Bollinger rules' numbers (config/backtest.yaml `rules: bollinger:`)."""

    period: int
    width: Decimal
    fast_minutes: int
    slow_minutes: int
    squeeze_lookback: int
    squeeze_window: int
    walk_lookback: int
    walk_min_closes: int
    walk_window: int
    slope_bars: int
    bounce_window: int

    def __post_init__(self) -> None:
        if self.period <= 1:
            raise ValueError("bollinger period must be more than one bar")
        if self.width <= 0:
            raise ValueError("bollinger width must be positive")
        for name in (
            "fast_minutes",
            "slow_minutes",
            "squeeze_lookback",
            "squeeze_window",
            "walk_lookback",
            "walk_min_closes",
            "walk_window",
            "slope_bars",
            "bounce_window",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"bollinger {name} must be at least one")
        if self.fast_minutes >= self.slow_minutes:
            raise ValueError("bollinger fast_minutes must be shorter than slow_minutes")
        if self.walk_min_closes > self.walk_lookback:
            raise ValueError("bollinger walk_min_closes cannot exceed walk_lookback")

    @property
    def history(self) -> int:
        """Bars any setup can look back from the signal bar, bands included."""
        reach = max(
            self.squeeze_window + self.squeeze_lookback,
            self.walk_window + self.walk_lookback,
            self.bounce_window,
            self.slope_bars,
        )
        return self.period + reach + 1


@dataclass(frozen=True, slots=True)
class Band:
    lower: Decimal
    middle: Decimal
    upper: Decimal

    @property
    def bandwidth(self) -> Decimal:
        return (self.upper - self.lower) / self.middle


def bands(bars: Sequence[Bar], period: int, width: Decimal) -> list[Band | None]:
    """One Band per bar: None until `period` bars exist."""
    out: list[Band | None] = []
    for i in range(len(bars)):
        if i + 1 < period:
            out.append(None)
            continue
        closes = [b.close for b in bars[i + 1 - period : i + 1]]
        mean = sum(closes, Decimal(0)) / period
        variance = sum(((c - mean) * (c - mean) for c in closes), Decimal(0)) / period
        spread = width * variance.sqrt()
        out.append(Band(mean - spread, mean, mean + spread))
    return out


def _band(bs: Sequence[Band | None], i: int) -> Band | None:
    return bs[i] if 0 <= i < len(bs) else None


def squeeze_fires(bars: Sequence[Bar], bs: Sequence[Band | None], s: BollingerSettings) -> Score:
    """Squeeze breakout on the last bar."""
    n = len(bars) - 1
    now = _band(bs, n)
    if now is None:
        return Score.NEUTRAL
    close = bars[n].close
    if close > now.upper:
        rising = True
    elif close < now.lower:
        rising = False
    else:
        return Score.NEUTRAL

    def outside(k: int) -> bool:
        b = bs[k]
        assert b is not None
        return bars[k].close > b.upper if rising else bars[k].close < b.lower

    for j in range(n - 1, max(n - s.squeeze_window, 0) - 1, -1):
        window = [_band(bs, k) for k in range(j - s.squeeze_lookback + 1, j + 1)]
        if any(b is None for b in window):
            break
        here = bs[j]
        assert here is not None
        if here.bandwidth <= min(b.bandwidth for b in window if b is not None):
            return Score.BULLISH if rising else Score.BEARISH
        if outside(j):
            return Score.NEUTRAL  # an earlier close outside after any squeeze came first
    return Score.NEUTRAL


def _first_since(touch: int, n: int, confirms: Callable[[int], bool]) -> bool:
    return confirms(n) and not any(confirms(k) for k in range(touch + 1, n))


def walk_fires(bars: Sequence[Bar], bs: Sequence[Band | None], s: BollingerSettings) -> Score:
    """Band-walk pullback on the last bar."""
    n = len(bars) - 1
    if n < 1 or _band(bs, n) is None:
        return Score.NEUTRAL
    for rising in (True, False):
        if _walk_at(bars, bs, s, rising, n, {}):
            return Score.BULLISH if rising else Score.BEARISH
    return Score.NEUTRAL


def _walk_at(
    bars: Sequence[Bar],
    bs: Sequence[Band | None],
    s: BollingerSettings,
    rising: bool,
    i: int,
    memo: dict[int, bool],
) -> bool:
    """Whether the walk fires in this direction on bar `i` (memoised: an entry bar is
    checked for having fired itself only when it is also a touch, which is rare)."""
    if i in memo:
        return memo[i]

    def confirms(k: int) -> bool:
        b = bs[k]
        if b is None or k < 1:
            return False
        if rising:
            return bars[k].close > b.middle and bars[k].close > bars[k - 1].high
        return bars[k].close < b.middle and bars[k].close < bars[k - 1].low

    def cancels(k: int) -> bool:
        b = bs[k]
        assert b is not None
        return bars[k].close < b.lower if rising else bars[k].close > b.upper

    fired = False
    for t in range(i - 1, max(i - s.walk_window, 0) - 1, -1):
        b = _band(bs, t)
        if b is None:
            break
        touched = bars[t].low <= b.middle if rising else bars[t].high >= b.middle
        if not touched:
            continue
        if confirms(t) and _walk_at(bars, bs, s, rising, t, memo):
            continue  # an entry bar does not arm a second entry (Andre, 2026-10-17)
        walked, last_walk = 0, t
        for k in range(t - s.walk_lookback, t):
            kb = _band(bs, k)
            if kb is None:
                continue
            if cancels(k):
                walked = 0  # a break resets the walk (Andre, 2026-10-17)
            elif (bars[k].close >= kb.upper) if rising else (bars[k].close <= kb.lower):
                walked, last_walk = walked + 1, k
        if walked < s.walk_min_closes:
            continue
        # a break anywhere after the walk cancels, before the touch bar included
        if any(cancels(k) for k in range(last_walk + 1, i + 1)):
            continue
        if _first_since(t, i, confirms):
            fired = True
            break
    memo[i] = fired
    return fired


def bounce_fires(bars: Sequence[Bar], bs: Sequence[Band | None], s: BollingerSettings) -> Score:
    """Band bounce with the trend on the last bar."""
    return _bounce_at(bars, bs, s, len(bars) - 1, {})


def _bounce_at(
    bars: Sequence[Bar],
    bs: Sequence[Band | None],
    s: BollingerSettings,
    n: int,
    memo: dict[int, Score],
) -> Score:
    """The bounce on bar `n` (memoised like the walk)."""
    if n in memo:
        return memo[n]
    now, before = _band(bs, n), _band(bs, n - s.slope_bars)
    if now is None or before is None:
        memo[n] = Score.NEUTRAL
        return Score.NEUTRAL
    if now.middle > before.middle:
        rising = True
    elif now.middle < before.middle:
        rising = False
    else:
        memo[n] = Score.NEUTRAL
        return Score.NEUTRAL

    def confirms(k: int) -> bool:
        b = bs[k]
        if b is None:
            return False
        bar = bars[k]
        inside = b.lower < bar.close < b.upper
        return inside and (bar.close > bar.open if rising else bar.close < bar.open)

    score = Score.NEUTRAL
    for t in range(n - 1, max(n - s.bounce_window, 0) - 1, -1):
        b = _band(bs, t)
        if b is None:
            break
        touched = bars[t].low <= b.lower if rising else bars[t].high >= b.upper
        if not touched:
            continue
        if confirms(t) and _bounce_at(bars, bs, s, t, memo) is not Score.NEUTRAL:
            continue  # an entry bar does not arm a second entry (Andre, 2026-10-17)
        if _first_since(t, n, confirms):
            score = Score.BULLISH if rising else Score.BEARISH
        break  # the latest touch decides
    memo[n] = score
    return score


Detector = Callable[[Sequence[Bar], Sequence[Band | None], BollingerSettings], Score]

SETUPS: dict[str, Detector] = {
    "squeeze": squeeze_fires,
    "walk": walk_fires,
    "bounce": bounce_fires,
}

_MINUTE = timedelta(minutes=1)


class BollingerTrigger:
    """A Bollinger setup as an EntryTrigger on `minutes`-long bars built from the
    one-minute bars: BULLISH (buy calls) or BEARISH (buy puts) when the setup fires on
    the bar that just completed, NEUTRAL otherwise."""

    def __init__(self, settings: BollingerSettings, setup: str, minutes: int) -> None:
        if setup not in SETUPS:
            raise ValueError(f"unknown bollinger setup {setup!r}")
        if minutes < 1:
            raise ValueError("bollinger timeframe must be at least one minute")
        self._s = settings
        self._setup = setup
        self._detect = SETUPS[setup]
        self._span = timedelta(minutes=minutes)
        self._minutes = minutes

    @property
    def agent_id(self) -> str:
        return "momentum"

    def read(self, bars: Sequence[Bar]) -> Read:
        require_series(bars, None, "BollingerTrigger bars")
        last = bars[-1]
        as_of = last.ts + _MINUTE
        need = self._s.history * self._minutes
        tf = (
            list(bars[-need:])  # one-minute rules read the completed minutes as they are
            if self._span == _MINUTE
            else resample(tuple(bars[-need:]), self._minutes, as_of)
        )
        name = f"bollinger {self._setup}"
        if not tf or tf[-1].ts + self._span != as_of:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, f"{name}: no bar just completed")
        tail = tf[-self._s.history :]
        score = self._detect(tail, bands(tail, self._s.period, self._s.width), self._s)
        what = {Score.BULLISH: "long", Score.BEARISH: "short"}.get(score, "no entry")
        return Read(self.agent_id, last.ts, score, f"{name}: {what}")
