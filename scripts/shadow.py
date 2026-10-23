"""Shadow driver: tonight's 1-minute futures bars -> the pre-registered London rule's signals.

Uses footprint.research.rules.london_orb (certified, PR 38) unchanged. Read-only; places nothing.
Usage: uv run python scripts/shadow.py BARS.csv SYMBOL VARIANT [--trades N --realized X --after HH:MM]
"""
import argparse
from datetime import time
from pathlib import Path

from footprint.research.bars import load_days
from footprint.research.engine import DayState
from footprint.research.rules.london_orb import build_orb
from footprint.research.settings import load_research

REPO = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser()
p.add_argument("bars"); p.add_argument("symbol"); p.add_argument("variant")
p.add_argument("--trades", type=int, default=0)
p.add_argument("--realized", type=float, default=0.0)
p.add_argument("--after", default="00:00")
a = p.parse_args()
s = load_research(REPO / "config", "nq.yaml")
rule = build_orb(s, a.variant)
days = load_days(Path(a.bars), a.symbol, s.zone, s.roll)
day = days[-1]
o = rule.settings
rng = rule._range(day, len(day))
print(f"session day {day.day}: {len(day)} bars, {day.clock[0]}..{day.clock[-1]} NY")
print(f"range {o.range_start:%H:%M}-{o.range_end:%H:%M}: {rng}")
h, m = map(int, a.after.split(":"))
state = DayState(a.trades, a.realized)
for i in range(len(day)):
    if day.clock[i] < time(h, m):
        continue
    sig = rule.entry(day, i, state)
    if sig is not None:
        print(f"SIGNAL bar {day.clock[i]} NY (closes +1 min) close {day.close[i]} "
              f"direction {sig.direction:+d} futures_stop {sig.futures_stop} flat_by {sig.flat_by:%H:%M}")
        break
else:
    print("no signal")
