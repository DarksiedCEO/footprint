# backtest-speed-v1 mutation notes (BH-11)

Spec: `qa/mutation/backtest-speed-v1.json` (44 mutants on the new window, cache and
search code). Groups: `windows` = tests/test_series_windows.py (every helper on a
BarWindow against the same bars as a list); `engine` = tests/backtest/test_engine.py
and test_engine_play.py (the decision window and the play caller's history never show
an open bar).

## Run 1 (`backtest-speed-v1-on-3b74e6f.out`): 42 mutants, 35 killed, 7 survived

Each survivor got a test in tests/test_series_windows.py (commit 8a5a27b):

| # | survivor | test that now kills it |
|---|---|---|
| 15 | session days: dates that go back searched as if in order | `test_days_out_of_order_in_a_series_are_grouped_as_for_a_list` |
| 25 | zero-day: a day with out-of-order clocks read by prefix | `test_the_zero_day_range_on_a_day_whose_clock_goes_back_inside_the_cutoff_hour` |
| 28 | zero-day: dates that go back searched as if in order | `test_days_out_of_order_in_a_series_are_grouped_as_for_a_list` |
| 31 | zero-day: holidays and weekends counted on the window path | `test_the_zero_day_range_skips_days_that_do_not_count_on_a_window_too` |
| 33 | zero-day: per-series days cached without zone and start | `test_one_series_read_with_two_prime_sessions_keeps_each_ones_days` |
| 36 | levels: minutes after the window counted present | `test_the_levels_snapshot_on_a_window_ending_inside_a_trend_bucket` |
| 37 | levels: closed-minute memo keyed by the wrong minute | `test_the_levels_closed_minute_memo_answers_as_session_at` |

The seven were re-run against the new tests before the commit (7 killed, 0 survived;
scratch run, not committed). The full spec was then run on the committed tree
(`backtest-speed-v1.out`).

## Argued equivalences (added to the spec on purpose, expected to survive)

- **M41, levels: the closed-minute memo never starts over.** `_closed` remembers
  `session_at(m) is CLOSED` per moment; the answer is a function of the moment and the
  reader's instrument alone. Clearing the memo at `_CLOSED_MEMO` entries only bounds
  memory: a cleared entry is recomputed to the same value. Never clearing changes no
  answer, only memory. (The memo answering as `session_at` after it starts over is
  pinned by `test_the_levels_closed_minute_memo_answers_as_session_at` with a cap of 7.)
- **M42, range days: no day of margin before today's local midnight.** `_from_day_before`
  searches today's after-close bars from (today's local midnight in UTC) minus one day.
  Without the margin the search starts at today's local midnight itself. Every bar on
  local date `today` is at or after that instant (argued from how zoneinfo maps a
  local midnight, not checked zone by zone; the shipped zone, Los Angeles, changes at
  02:00): when
  midnight is repeated, `datetime.combine` takes the first (fold 0); when it is skipped,
  the time maps to the transition instant, the first one of the date; when a date goes
  back (St. John's, 00:01 to 23:01), the first minute of the new date is still after
  its own midnight. The margin is defensive only; the bars it adds are filtered out by
  the unchanged date test. Not killable by an output check.

## Final run (`backtest-speed-v1.out`, on 8a5a27b, tree clean)

44 mutants: 42 killed, 2 survived: M41 and M42, the two argued equivalences above.

## After the independent review (CERTIFIED WITH CONDITIONS, head 689321b)

The reviewer's mutants R7, R1 and R5 survived every test. Each now has a test
(tests/test_series_windows.py) and a mutant in the spec:

| reviewer | mutant | test that kills it (failed first under the mutant) |
|---|---|---|
| R7 (F1) | zero-day: `days.highs[k] <= hi + 1` (a day one bar past the window taken whole) | `test_a_window_one_bar_short_of_a_days_last_bar_never_sees_it` (window vs list, `_window_spans` with late cutoffs and the public `check()` at a mid-minute `as_of`) |
| R1 (F3) | clock index from the first series ever seen | `test_two_series_alive_keep_their_own_clock_index` |
| R5 (F3) | closed-minute memo shared by every reader and instrument | `test_two_levels_readers_on_two_instruments_keep_their_own_closed_minutes` |

F4 (hardening): `BarSeries` has slots and read-only properties (`bars`, `starts` now a
tuple, `timeframe_minutes`); `BarWindow` refuses setting or deleting attributes (a
tuple subclass cannot have slots, so its `__dict__` exists; writing to it directly is
the only way round, and nothing does). Cost: none measurable. Interleaved runs of the
130-session timing file on a busier box: 689321b 46.8 s and 44.5 s; with F4 43.8 s,
43.4 s and 48.5 s.

## Final run after the review (`backtest-speed-v1.out`, on a3bb464, tree clean)

47 mutants: 45 killed, 2 survived: M41 and M42, the argued equivalences above. The
previous final run (44 mutants on 8a5a27b) is kept as `backtest-speed-v1-on-8a5a27b.out`.
