# PR regime-v1: mutation notes

Evidence: `regime-v1.out` (spec `regime-v1.json`). First run: 32 mutants, 9 survived.
Seven got tests; two are argued equivalent below and kept out of the spec.

## Survivors that got tests
| Mutant | Test that now kills it |
|---|---|
| efficiency history one day too long | `test_efficiency_rank_uses_only_its_own_lookback` |
| overnight median over the whole lookback | `test_overnight_median_uses_only_the_overnight_lookback` (nights reworked) |
| mean not median | same test (mean 45 vs median 10) |
| flat history day skipped instead of refused | `test_a_flat_history_opening_stands_aside_and_is_named` |
| coverage check removed | `test_a_thin_opening_without_long_gaps_stands_aside` |
| gap check removed | `test_one_long_gap_overnight_stands_aside_despite_good_coverage` |
| start gap ignored | `test_a_late_start_overnight_stands_aside` |

## Argued equivalent (kept out of the spec)
- **`completed(minute_bars, as_of, 1)` replaced by all bars (lookahead).** Every window the
  reader reads ends at or before the decision time (the opening ends exactly at it; the
  overnight ends at the cash open), and the reader returns before reading any window
  when `as_of` is earlier than the decision time. A bar not complete at `as_of` has
  `ts > as_of - 1 minute >= decision - 1 minute`, so `ts >= decision` (bars sit on whole
  minutes), which is outside every half-open window `[begin, end)` with `end <= decision`. `completed` stays as a second guard
  and documents the no-lookahead rule; `test_bars_not_yet_complete_are_ignored` and
  `test_later_in_the_day_the_label_is_unchanged` pin the behaviour.
- **The empty-window check removed.** `min_session_coverage_pct` is loaded as a number
  > 0 and every window spans at least one minute, so `need > 0` and an empty window
  (0 bars) always fails the coverage check next, with the same message ("0 of N minutes").
