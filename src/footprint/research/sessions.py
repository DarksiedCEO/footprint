"""Named sessions of the trading day, by New York wall-clock time (config/research.yaml).

Each window is [start, end) and may wrap midnight. A minute no window covers is labelled
`other`. The loader refuses overlapping windows, so a minute has exactly one label. A
trade is labelled with the session of the bar it ENTERS on.
"""

from __future__ import annotations

from datetime import time

from footprint.research.settings import OTHER, Window


def classify(clock: time, sessions: tuple[tuple[str, Window], ...]) -> str:
    """The name of the session covering `clock`, or `other`."""
    for name, window in sessions:
        if window.covers(clock):
            return name
    return OTHER


def names(sessions: tuple[tuple[str, Window], ...]) -> tuple[str, ...]:
    """Every label a trade can carry, in config order, `other` last."""
    return (*(name for name, _ in sessions), OTHER)
