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
