# Backtest readout

**Verdict: NOT ENOUGH TRADES**

- all trades: 1 (need >= 200)
- out of sample trades: 0 (need >= 60)

Data: 2026-09-15T14:00:00+00:00 to 2026-09-15T14:15:00+00:00. Equity 25000 to 24230.00. Ledger and journal agree: yes.
Decisions: 11 (1 fired). Alarms: 2. Ended early: no.
Drawdown marked to the bid: 3.08% (770.00). Worst open loss in any trade: -770.00.

## Gates

| Segment | Gate | Required | Actual | Result |
|---|---|---|---|---|
| all | trades | >= 200 | 1 | FAIL |
| out of sample | trades | >= 60 | 0 | FAIL |
| all | win rate % | >= 45 | 0.00 | FAIL |
| all | payoff ratio | >= 1.5 | n/a | FAIL |
| all | profit factor | >= 1.3 | 0.00 | FAIL |
| all | max drawdown % | <= 15 | 3.08 | pass |
| out of sample | win rate % | >= 45 | n/a | FAIL |
| out of sample | payoff ratio | >= 1.5 | n/a | FAIL |
| out of sample | profit factor | >= 1.3 | n/a | FAIL |
| out of sample | max drawdown % | <= 15 | 0.00 | pass |

## Metrics

| | All | In sample | Out of sample |
|---|---|---|---|
| trades | 1 | 1 | 0 |
| wins | 0 | 0 | 0 |
| losses | 1 | 1 | 0 |
| win_rate_pct | 0.00 | 0.00 | n/a |
| avg_win | n/a | n/a | n/a |
| avg_loss | 770.00 | 770.00 | n/a |
| payoff_ratio | n/a | n/a | n/a |
| profit_factor | 0.00 | 0.00 | n/a |
| net_pnl | -770.00 | -770.00 | 0.00 |
| expectancy | -770.00 | -770.00 | n/a |
| max_drawdown_pct | 3.08 | 3.08 | 0.00 |
| max_drawdown | 770.00 | 770.00 | 0.00 |

## Exit reasons

- STOP: 1

## Play caller (one label per trading day; only TREND trades)

- no day's label was journaled

## Assumptions (read every result through these)

- option prices: MODELLED from the futures price (no options history)
- delta (G4, fixed): 0.45
- option model atm_premium_points: 150
- option model spread_points: 2
- option model strike_step: 25
- option model strikes_each_side: 6
- option model open_interest: 500
- option model expiries_listed: 5
- option model trading_class: Q3C
- fees per contract per side: 2.5
- risk.profit_target: 1000
- risk.trail_keep_pct: 50
- risk.stop_loss: 750
- theta.decay_exit.enabled: false
- theta.decay_exit.window_minutes: 30
- theta.decay_exit.max_pct_of_open_profit: 50
- news blackouts: NONE supplied
- intrabar path: adverse-first against an open trade
- bars: <TMP>/bars.csv
