# Play caller (branch playcaller-v1): mutation notes

Evidence: `playcaller-v1.out` (spec `playcaller-v1.json`).

History: first built on branch `regime-v1` (ab6f2dd/e83e96d; pushed, no PR, superseded:
its rebase onto the Black-76 fixes was not possible without a force-push). Independent
review of e83e96d: NOT CERTIFIED (HIGH-1, MEDIUM-1..4, LOWs). This branch carries the
rework; the reviewer's 18 extra mutants are folded into the spec (adapted to the new
code), plus mutants for every new check.

## Survivors that got tests (all rounds)
| Mutant | Test that kills it |
|---|---|
| efficiency / overnight history one day too long | `test_efficiency_rank_uses_only_its_own_lookback`, `test_overnight_median_uses_only_the_overnight_lookback` |
| mean not median | `test_overnight_median_uses_only_the_overnight_lookback` (median 15, mean 25) |
| flat history day skipped | `test_a_flat_history_opening_stands_aside_and_is_named` |
| coverage / gap / start-gap / end-gap checks | `test_a_thin_opening_without_long_gaps_stands_aside`, `test_one_long_gap_overnight_stands_aside_despite_good_coverage`, `test_a_late_start_overnight_stands_aside`, `test_a_stall_right_before_the_cash_open_makes_the_overnight_unusable` |
| gap / coverage boundaries | `test_a_gap_of_exactly_the_limit_is_usable`, `test_coverage_exactly_at_the_minimum_is_usable` |
| net move not absolute (down days) | `test_a_clean_falling_opening_is_a_trend_too` |
| overnight last minute required | `test_the_overnight_does_not_need_its_last_minute` |
| overnight start day / zone | `test_a_same_day_overnight_starts_on_the_day_itself`, `test_the_overnight_window_uses_its_own_time_zone` |
| overnight range drops first / last minute | `test_the_overnight_range_includes_its_first_and_last_minutes` |
| trend 100 refused | `test_a_trend_rank_of_100_is_a_valid_setting` |
| prime check reduced to `today is None` | `test_the_overnight_session_is_not_a_place_to_label` |
| calendar search failure not caught | `test_no_trading_day_in_the_search_window_stands_aside` |

## Argued equivalent (kept out of the spec)
- **`completed()` replaced by all bars.** Every window ends at or before the decision
  time and the reader returns before reading any window when `as_of` is earlier. A bar
  not complete at `as_of` has `ts > as_of - 1 minute >= decision - 1 minute`, so on a
  whole minute `ts >= decision`, outside every half-open window `[begin, end <= decision)`.
  An off-minute bar is refused by the whole-minute check. Pinned by
  `test_bars_not_yet_complete_are_ignored`.
- **The empty-window check removed.** `min_session_coverage_pct` is loaded as > 0 and a
  window spans at least one minute, so an empty window fails the coverage check next,
  with the same message ("0 of N minutes").
- **CHOP guarded by `night >= 0`** (reviewer's mutant 11): always true, a range is a max
  high minus a min low and `Bar` enforces high >= low.
- **A kept label looked up without the day** (`next(iter(...))`, reviewer round 2): the
  kept entry is reused only when its key equals today's overnight and opening bars, and
  another day's key holds that day's bars (different timestamps), so it can never match
  today's: the lookup by day changes nothing observable.
- **Cache key one bar earlier than the night open** (reviewer round 3): the bar before
  the overnight open belongs to the previous trading day's session and changes only with
  a different data set, which also changes today's bars; on its own it can only cause a
  recompute (never a stale label), and the recompute gives the same answer.
- **Cache key built from all bars instead of completed ones** (reviewer round 3, and the
  current code): the key window ends at the decision time and a label is asked for only
  at or after it, so every bar in the window has closed; the completed filter would drop
  nothing. The code now bisects the input directly for speed (round-3 MEDIUM-R3-2).
- **Reviewer round 2's four equivalents:**
  - `cash.end <= cash.start` dropped from the decision bound: then the session length is
    <= 0 and `decision_minutes >= 1` (loader), so the second clause already raises.
  - `today is None` dropped: PRIME is only ever returned with a trading day.
  - The kept-label dict growing to every day instead of one entry: memory only; a lookup
    is by today's date and key, so older entries are never served.
  - An extra `as_of >= decision` guard on the lookup: the call already returned above
    when `as_of < decision`.
- **Reviewer round 2's contrived survivor:** an order check over only the last 30,000
  bars survives because the test history is about 10,000 bars; the swap test already
  disorders an early day. Kept out of the spec (it would need a 30,000-bar fixture to
  kill a change nobody would make).
