# PR 37 (research-v1): mutation notes

Evidence: `research-v1.out` (spec `research-v1.json`), run on the PR's own commit.

## Trial run (before the commit) and what became of each survivor

The trial ran 60 mutants on the uncommitted tree: 55 killed, 3 survived, 2 errored.

| Mutant | Trial | Resolution |
|---|---|---|
| 1 roll minutes ignored | SURVIVED | Test added: `test_session_day_reads_the_roll_minutes` (roll 17:30). Killed in the evidence run. |
| 35 scale-out target exclusive | SURVIVED | Test added: `test_scale_out_target_reached_exactly_fills` (exactly +$1,000 net). Killed. |
| 43 rule asked on the exit bar (`i = closed_in`) | ERROR | An entry on the day's last bar closes in that same bar (`closed_in == i`), so the mutant never advances `i` and loops, appending trades without end (`test_entry_on_the_last_bar_is_valued_and_flattened_at_once`). The test process ended with an exit code other than 1 (cause not verified; most likely memory exhaustion, since the mutation runner reports a timeout separately). An ERROR is not a kill, so the mutant was rewritten to a variant that terminates (`i = closed_in if closed_in > i else closed_in + 1`). Killed by `test_one_position_at_a_time_and_next_entry_after_the_exit_bar`. |
| 44 skipped day retried (`break` -> `continue`) | ERROR | `continue` without advancing `i` asks the same bar again forever (same failure mode as 43). Rewritten to advance (`i += 1; continue`): the day is then retried on later bars and counted skipped more than once. Killed by `test_day_without_usable_vol_is_skipped_and_counted`. |
| 50 t-stat with one trade (`n > 1` -> `n > 0`) | SURVIVED | Equivalent: with one trade the population stdev is 0, and the `sd > 0` check already returns None. The guard was simplified to `if pnls:` (the stdev check alone decides), and the mutant was dropped from the spec. Mutant 49 (`sd > 0` -> `sd >= 0`) pins the remaining check. |

The evidence run has 62 mutants: the 59 above plus three on the gate reader added when
research stopped importing footprint.backtest (a 100% gate accepted, zero min_trades
accepted, profit factor read from the payoff key). Result: 62 killed, 0 survived or
errored, on ed493a1 with a clean tree.

## Review round 1 (24be374, NOT CERTIFIED) and what became of it

The reviewer's surviving mutants are in the spec and now die:
- 37-H1 strike chosen at the data's own level, and no strike rounding (pricing tests:
  equal dollars on the same percentage path at several levels, rounding on);
- 37-M1 next day's vol (`test_the_vol_of_the_day_before_prices_the_trade`);
- 37-M2 verdict on sorted trades (`test_the_verdict_reads_trades_in_their_order_not_sorted`);
- 37-L1 `realized = net` (`test_green_stop_input_sums_every_closed_trade`, 3 trades);
- 37-L3 stop at the entry price accepted (`test_a_futures_stop_at_the_entry_price_is_refused`).
New code (marked drawdown, ruin, marks) has its own mutants. Four old mutants were
re-pointed at the moved code (moneyness sign, forward not rescaled, target gap fill,
drawdown pct never computed); their intent is unchanged.

Trial on the fix tree: 80 mutants, 78 killed, 2 survived, both argued equivalent and
dropped:
- **`peak > 0` -> `peak != 0`, in `summarize` and in `marked_drawdown`** (the reviewer's
  37-L2 mutant). With a negative peak, (peak - x) / peak is <= 0 because peak - x >= 0,
  so `max(worst_pct, ...)` never changes; a peak of exactly 0 is excluded by both. The
  mutant is behaviourally identical. The negative-peak branch itself is pinned by the
  killed `peak < 0` mutant and by `test_drawdown_pct_after_equity_falls_below_zero` and
  `test_marked_drawdown_pct_stops_at_a_non_positive_peak`.

The reviewer's other equivalent survivors (37-I2: peak `>=`, default Decimal rounding,
the session filter rewrite, `bars._day` UTC conversion) are accepted as equivalent and
not in the spec.

Evidence after review round 1 (on branch research-v1): 78 mutants, 78 killed, 0 survived or errored, on 18491d5 with a clean tree.

## Review round 2 (b78e80b, CERTIFIED WITH CONDITIONS)

- 37-R2-M1: the reviewer's N5 (`equity += trade[-1]` -> `trade[0]`) is in the spec, killed
  by `test_marked_drawdown_carries_each_trades_final_net_not_its_first_mark` (marks
  starting at -170, exact dollars and percent).
- 37-R2-L1: the reviewer's N3 (a mark recorded at a fill point) is pinned rather than
  argued: `test_no_mark_is_recorded_at_a_fill_point` fixes the marks of a stopped trade
  (entry, the bar's open, the final net). The reviewer's observation stands that the
  extra mark could not change a drawdown (at a full exit it equals the final net; at a
  scale-out target it lies between its neighbours), so the test pins the documented
  shape of `marks`, not a number the verdict reads.
- 37-R2-L2: config/research.yaml's moneyness comment now says points at the reference
  price level.
- 37-R2-I1 (info, documented here): ruin is judged on realized equity; marked equity can
  go below zero inside a trade without a RUIN line. The marked drawdown gate fails long
  before that, so the verdict is unaffected.
