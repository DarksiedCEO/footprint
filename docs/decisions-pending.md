# Decisions pending (Andre)

Places where the build had to interpret or deviate from the approved spec.
Each is a config value or a one-line change; nothing here is hidden in code.

| # | Where | What the build does | Why | Your call |
| --- | --- | --- | --- | --- |
| D1 | Grant (levels) | Round numbers are reported but are not walls | With one contract N ≈ 111 pts, more than the 100-pt spacing, so the next round number is always inside the target and every trade would be vetoed | Keep, or make round numbers walls (and accept far fewer trades) |
| D2 | Nia (zero-day) | Volume tripwire at 10x typical, not G9's 4x | A 3–4x volume spike is Darius's own entry trigger; 4x would veto the strategy's best entries | Confirm 10x or pick another |
| D3 | Grant / VWAP | Effective entry window is 06:31–13:55 PT, not 04:00–13:55 | VWAP is anchored at the cash open (G10); with no VWAP the VWAP side rule and levels fail closed before 06:30. After the 13:00 cash close, range uses the finished cash day | Keep, or anchor VWAP at the 4:00 prime start to allow pre-market entries |
| D4 | Nadia (range) | "Already-stretched day" = at least 70% of the ADR used | The spec doesn't define it; 70% is the edge of the quiet band | Confirm or change `range.quiet_below_pct` usage |
| D5 | Grant / Nia | Feed counts as stale if the newest closed bar is over 120 s old; a session with under 90% of its minutes is unusable | Fail closed on bad data instead of trading on it | Confirm `data.max_bar_age_seconds` and `data.min_session_coverage_pct` |
| D6 | Calendar | Early closes are not modelled; the holiday list is empty; on an early-close day the close lead still counts from 14:00 | Grant fails closed for the whole day after an early close (Black Friday, Jul 3, Dec 24) or an unlisted holiday, because the prior session looks incomplete. Safe, but it blocks those days | Before paper trading: fill `holidays` and add early closes from the CME calendar |
| D7 | Fees (G15) | $2.50 per contract per side | UNVERIFIED estimate of IBKR commission + CME fees | Replace with the figure from IBKR's fee schedule for NQ options |
| D8 | Felix (G3) | Minimum open interest 100 | G3 says "calibrate from data"; there is no chain data yet | Calibrate from a week of chain snapshots |
| D9 | Instrument | Option tick 0.25 points | UNVERIFIED against the CME contract specs | Confirm the NQ option tick (and any reduced tick for cheap premiums) |
| D10 | Booker (G1) | Resting stop-limit's limit 20 ticks (5 points, $100/contract) below the trigger | G1 approved "a wide limit" without a number | Confirm or change `stop_order.limit_offset_ticks`; test in paper |
| D11 | Ezra / Diego (G5) | Entry prices are on the tick: start at the mid rounded down, never above mid + 25% of the spread rounded down. With a one-tick spread that means posting at the bid only | G5 forbids paying past mid + 25%; on a one-tick spread no tick price exists between the bid and that limit | Keep strict, or allow the ask on one-tick spreads |
| D12 | Felix (G3) | "1–3 DTE" = calendar days from the trading day (an overnight session counts from the day it leads into) | Avoids buying an option that expires inside the session it is traded in | Confirm calendar days (vs trading days) |
| D13 | Booker (G7, G15) | The trail starts at +$1,000 NET of round-trip fees and locks 50% of the net peak | G15: every P&L figure the risk layer sees includes fees | Confirm |
| D14 | Felix | Skip if the nearest strike is more than 50 points from the underlying | Guards against a gappy chain; not in the spec | Confirm or change `contract.max_strike_distance` |
| D15 | Darius (momentum) | Follow-through = the next bar CLOSES beyond the spike bar's CLOSE | The spec says "a confirming follow-through bump the same direction"; beyond the spike's high is the stricter reading | Close beyond the spike's close, or beyond its high? |
| D16 | Nia | Bar tripwires check each of the last 5 closed bars; the trigger's spike candle (the bar before the last) is judged for jump/volatility at 8x, not 4x | Without the window, an extreme spike one minute old no longer tripped her and the trade fired (found by test). Without the 8x, a big spike candle (the strategy's own trigger) would trip her | Confirm `zeroday.recent_bars` and `zeroday.spike_bar_trip_multiple` |
| D17 | Runner (failsafe) | A position not managed for 30 s is flattened through the broker alone | Guards against inputs or steps that keep failing; not in the spec | Confirm `runner.max_unmanaged_seconds` |
| D18 | Reconcile | A disagreement, and what is held after the cancels, must hold still 2 s before reconcile acts; a report that will not hold still for 10 s escalates and halts; a cancel unconfirmed for 10 s escalates | A live broker's position report and order list can lag; acting on a lagging read can sell twice | Confirm `reconcile.settle_seconds`, `max_settle_seconds`, `cancel_escalate_seconds`; re-measure against IBKR paper |
| D19 | Alarms | Retry every 5 s while the line is down; keep at most 500 waiting (oldest dropped and counted) | Not in the spec | Confirm `alarms.retry_seconds`, `alarms.max_pending` |
| D20 | Alarms | Which channel is the primary alarm line (push, SMS, email) and which is the fallback | The code supports a fallback line; the channels are a deployment choice | Pick both channels |
| D21 | Exits on a dead feed | With no fresh quote, a closing exit (and reconcile's flatten) walks its sell limit down one tick a step, to a floor of one tick | Never rest on a dead quote. The cost: a resting sell at a walked-down limit fills AT that limit when the market returns (the exchange trades at the resting order's price), so a long gap can give the option away cheaply | Keep, or put a floor under the walk (e.g. a share of the last known bid) and escalate instead |
| D22 | Backtest option model | ATM premium 150 points at 1 day (time value falls with the square root of time), spread 2 points, strikes every 25 points, daily expiries, fixed delta 0.45, no gamma | No historical NQ options data; every number is UNVERIFIED and printed in each readout | Check against real NQ option quotes (a week of snapshots) before trusting any backtest result |

## Before paper trading (from the independent reviews)

1. Verify the NQ option tick (D9) and the per-contract fee (D7). An off-tick fill cannot carry a stop and is flattened at once.
2. Fill in the CME holidays and early closes (D6).
3. DONE in PR #8 (runner) and PR #9 (order path); kept for the record. The runner PR had to:
   - make the simulated broker fill against the same quote the decision used (review R12). Until then, no backtest or paper P&L is trustworthy;
   - call `manage` every few seconds while a position or exit order exists;
   - call `reconcile` until it returns True at startup and on any doubt;
   - send every alarm and halt to Andre;
   - after a restart, call `reconcile(None)`; entries resume only after Andre clears the kill switch;
   - include integration tests against an IBKR-style stub: cancels acknowledged late, orders sent but never acknowledged, a rejected modify that leaves the original working, and broker exceptions on every call.

## Order-path limits that need Andre's written sign-off (PR #9)

The order path never sells twice, never sells short and never books a guessed price. Two situations are deliberately left to Andre, because any automatic sale in them could go short (a short option has unlimited risk):

| # | Situation | What the system does | Worst case |
|---|---|---|---|
| L1 | The broker's position report will not hold still (it flickers or keeps changing) for 10 s | Sells nothing on the untrusted report; halts; latches the kill switch; alarms "flatten by hand" once; keeps checking and acts as soon as it settles | The position is held, protected only by its resting stop, possibly past the close |
| L2 | An order (a stop or an exit) was sent, its answer was lost, and the resend under the same id also failed, so nobody can say whether it is working | Counts it as working; sends nothing beside it; reconcile cancels and re-prices it every round and escalates "CANCEL STUCK ... flatten by hand" after 10 s | A long option held, possibly without a stop: the loss is limited to the premium paid |

L2 goes away in the IBKR PR with OCA groups (the broker itself guarantees only one of a position's stop and exits can fill).

## Acceptance conditions for the IBKR PR (from the PR #8 and PR #9 reviews)

1. Positions reported consistently with fills (derive them from executions, or wait for the position update).
2. A resend under the same id can never create a second order; map IBKR's duplicate-id and "cannot modify a filled order" errors to `DuplicateOrderId`. Test both.
3. Load every open order (all client ids, `reqAllOpenOrders`) before the first reconcile after a restart.
4. Put each position's stop and exits in one OCA group (ocaType=1) so only one can fill.
5. Re-confirm reconcile's stale-price walk-down against the real adapter.
6. Order and position ids become UUIDs (a restart restarts the sequence).
7. Integration tests against an IBKR-style stub: late cancels, lost acknowledgements, rejected modifies, partial fills, reordered quotes, lagging position reports, a flickering position report.
