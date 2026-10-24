"""Signal parity: the harness noise_band rule against tournament.py's
noise_band_x1.5_every15, first signal per day, on one bars file. Signals only
(date, direction, entry minute); no P&L is computed or printed.

    uv run python qa/research/noiseband_parity.py TOURNAMENT_PY BARS_CSV [INSTRUMENT]

TOURNAMENT_PY is the source script (its sha256 is printed; the pre-registration names
4c0db0b7...). The tournament's own `load` and entry function make the expected list;
the loop that takes the first signal is its `run` loop stopped at the first trade.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import sys
from datetime import datetime
from pathlib import Path

from footprint.research.bars import load_days
from footprint.research.engine import DayState
from footprint.research.rules.noise_band import build_noise
from footprint.research.settings import load_research

REPO = Path(__file__).resolve().parents[2]
VARIANT = "noise_band_x1.5_every15"

Signal = tuple[object, int, int]


def tournament_signals(source: Path, bars: str) -> list[Signal]:
    sys.argv = ["tournament"]  # the script reads optional numbers from argv at import
    spec = importlib.util.spec_from_file_location("tournament", source)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {source}")
    tour = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tour)
    days = tour.load(bars)
    entry = tour.ENTRIES[VARIANT]
    out: list[Signal] = []
    hist: list[object] = []
    for day in days:
        if math.isnan(day.adr) or math.isnan(day.prev_close):
            hist.append(day)
            continue
        signal, _ = entry(day, hist)
        k = 0
        while k < len(day.c) - 1:
            if day.m[k] >= tour.FLAT_MINUTE:
                break
            d = signal(k, 0)
            if d:
                out.append((day.date, d, day.m[k + 1]))
                break
            k += 1
        hist.append(day)
    print(f"tournament: {len(days)} kept days, {len(out)} first signals")
    return out


def harness_signals(bars: str, instrument: str) -> list[Signal]:
    s = load_research(REPO / "config", instrument)
    days = load_days(Path(bars), s.symbol, s.zone, s.roll)
    rule = build_noise(s, None)
    rule.prepare(days)
    start = rule.settings.rth_start
    fresh = DayState(0, 0.0)
    out: list[Signal] = []
    for day in days:
        for i in range(len(day)):
            sig = rule.entry(day, i, fresh)
            if sig is not None:
                opened = datetime.combine(day.day, day.clock[i + 1])
                minute = (opened - datetime.combine(day.day, start)).seconds // 60
                out.append((day.day, sig.direction, minute))
                break
    print("\n".join(rule.report(days)))
    print(f"harness: {len(days)} session days, {len(out)} first signals")
    return out


def main() -> int:
    source, bars = Path(sys.argv[1]), sys.argv[2]
    instrument = sys.argv[3] if len(sys.argv) > 3 else "nq.yaml"
    print(f"tournament.py sha256 {hashlib.sha256(source.read_bytes()).hexdigest()}")
    print(f"bars {bars}; instrument {instrument}")
    expected = tournament_signals(source, bars)
    got = harness_signals(bars, instrument)
    e, g = set(expected), set(got)
    both = e & g
    union = len(e | g) or 1
    print(f"agree: {len(both)}; only tournament: {len(e - g)}; only harness: {len(g - e)}")
    print(f"agreement: {len(both)} of {len(e | g)} signal days ({len(both) / union:.6f})")
    by_e = {d: (x, m) for d, x, m in expected}
    by_g = {d: (x, m) for d, x, m in got}
    for d in sorted(set(by_e) | set(by_g)):  # type: ignore[type-var]
        if by_e.get(d) != by_g.get(d):
            print("MISMATCH", d, "tournament", by_e.get(d), "harness", by_g.get(d))
    print(f"same list in the same order: {expected == got}")
    return 0 if expected == got else 1


if __name__ == "__main__":
    sys.exit(main())
