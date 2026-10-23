# London shadow test — night of Thu 2026-10-22 → Fri 2026-10-23

Read-only. No orders. Rule: L1 London opening-range breakout (pre-registered, docs/research/london-v1.md, PR 38), green-stop variant $500.
Signals come from the certified rule code, fed by tonight's live IBKR 1-minute bars. P&L comes from REAL IBKR option quotes (ask in, bid out), not a model.
Option: at-the-money Oct 30 (European end-of-month). The pre-registered 7-day choice is the closest listed European expiry, 7.5 days.
Deviation, noted: the pre-registered 1-day leg would be the Oct 23 weekly (~0.5 day at 03:15 ET), and its call spread was 28 pts tonight. It is recorded only if the spread is sane at entry.
NQ is the pre-registered market. ES is exploratory (Spartan preview) and is not part of the judged test.
Check-ins every ~15 min from 03:17 ET until 08:30 ET.

## Log
- 23:27 ET: spreads measured (spreads-2026-10-22.md). Driver smoke-tested on synthetic bars: signal found correctly.
- 2026-10-22 ~20:50 PT: driver committed to this branch (scripts/shadow.py, repo-relative paths). Re-smoke-tested on london-v1 code: range 03:00-03:15 found, long signal on breakout, flat_by 08:25.
