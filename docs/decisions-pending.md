# Decisions pending (Andre)

Places where the build had to interpret or deviate from the approved spec.
Each is a config value or a one-line change; nothing here is hidden in code.

| # | Where | What the build does | Why | Your call |
| --- | --- | --- | --- | --- |
| D1 | Grant (levels) | Round numbers are reported but are not walls | With one contract N ≈ 111 pts, more than the 100-pt spacing, so the next round number is always inside the target and every trade would be vetoed | Keep, or make round numbers walls (and accept far fewer trades) |
| D2 | Nia (zero-day) | Volume tripwire at 10x typical, not G9's 4x | A 3–4x volume spike is Darius's own entry trigger; 4x would veto the strategy's best entries | Confirm 10x or pick another |
| D3 | Grant / VWAP | No entries before the 6:30 PT cash open | VWAP is anchored at the cash open (G10); with no VWAP the VWAP side rule and levels fail closed | Keep, or anchor VWAP at the 4:00 prime start to allow pre-market entries |
| D4 | Nadia (range) | "Already-stretched day" = at least 70% of the ADR used | The spec doesn't define it; 70% is the edge of the quiet band | Confirm or change `range.quiet_below_pct` usage |
| D5 | Grant / Nia | Feed counts as stale if the newest closed bar is over 120 s old; a session with under 90% of its minutes is unusable | Fail closed on bad data instead of trading on it | Confirm `data.max_bar_age_seconds` and `data.min_session_coverage_pct` |
| D6 | Calendar | Early closes are not modelled; the holiday list is empty | Grant fails closed for the whole day after an early close (Black Friday, Jul 3, Dec 24) or an unlisted holiday, because the prior session looks incomplete. Safe, but it blocks those days | Before paper trading: fill `holidays` and add early closes from the CME calendar |
| D7 | Fees (G15) | $2.50 per contract per side | UNVERIFIED estimate of IBKR commission + CME fees | Replace with the figure from IBKR's fee schedule for NQ options |
| D8 | Felix (G3) | Minimum open interest 100 | G3 says "calibrate from data"; there is no chain data yet | Calibrate from a week of chain snapshots |
| D9 | Instrument | Option tick 0.25 points | UNVERIFIED against the CME contract specs | Confirm the NQ option tick (and any reduced tick for cheap premiums) |
| D10 | Booker (G1) | Resting stop-limit's limit 20 ticks (5 points, $100/contract) below the trigger | G1 approved "a wide limit" without a number | Confirm or change `stop_order.limit_offset_ticks`; test in paper |
| D11 | Ezra / Diego (G5) | Entry prices are on the tick: start at the mid rounded down, never above mid + 25% of the spread rounded down. With a one-tick spread that means posting at the bid only | G5 forbids paying past mid + 25%; on a one-tick spread no tick price exists between the bid and that limit | Keep strict, or allow the ask on one-tick spreads |
| D12 | Felix (G3) | "1–3 DTE" = calendar days from the trading day (an overnight session counts from the day it leads into) | Avoids buying an option that expires inside the session it is traded in | Confirm calendar days (vs trading days) |
| D13 | Booker (G7, G15) | The trail starts at +$1,000 NET of round-trip fees and locks 50% of the net peak | G15: every P&L figure the risk layer sees includes fees | Confirm |
| D14 | Felix | Skip if the nearest strike is more than 50 points from the underlying | Guards against a gappy chain; not in the spec | Confirm or change `contract.max_strike_distance` |
