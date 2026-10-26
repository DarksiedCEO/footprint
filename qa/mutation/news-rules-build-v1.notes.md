# news-rules-build-v1: mutation notes (review round 1)

Spec: `qa/mutation/news-rules-build-v1.json` (the 42 round-0 mutants, the three that
named `time_stop_after_daily` rewritten for the per-window switch, plus the round-1
mutants: C2, C3, M3 and the reviewer's survivors R1-R5, R9, R12).

## Survivors argued equivalent

**R4: `if end < start:` changed to `if end <= start:` in `NewsRules._pre_news`.**
`end` can never equal `start`:
- `start` is the prime session's start on a whole minute (`_clock` refuses seconds and
  microseconds), converted to UTC through a zone whose offset is whole minutes on every
  date the calendar can hold (2010 on; no zone in use has had a sub-minute offset since
  the 19th century).
- `end` is `_end_of_minute(...)`: a whole minute plus one minute minus one microsecond,
  so its microsecond field is always 999999.
So `end == start` is impossible and `<` and `<=` refuse exactly the same days. The
nearest real boundary (end one microsecond before the prime start, refused; a window
ending in the prime start's own minute, accepted) behaves the same under both.

**R9: `start > session.end` changed to `start >= session.end` in `NewsRules._overnight`.**
`__init__` refuses `overnight_flat_from == overnight_session.end` (ConfigError, tested
by `test_n_open_3_a_start_outside_the_overnight_session_is_refused[03:59]`), so
`_overnight` never runs with `start == session.end`; the two comparisons agree on every
value that reaches it.

## Survivors killed by new tests (were round-1 survivors)

- R1 (`now.date()`): `test_windows_are_the_news_clock_days_around_the_moment` (ET
  evening; a Tokyo news clock, whose date runs ahead of UTC's).
- R2 (non-wrapping session start excluded): `test_an_overnight_session_not_crossing_midnight`.
- R3 (CSV first): `test_the_scheduled_windows_come_before_the_csv_ones` (order pinned:
  the scheduled window is the one named when they overlap).
- R5 (last calendar day not checked): `test_the_last_calendar_day_is_checked_at_start_up`
  (a London news clock empties N4 on 2026-03-29 only, the calendar's last day there).
- R12 (time-stop look-back end exclusive):
  `test_an_entry_exactly_at_the_end_of_the_time_stop_window_gets_it`.

## Round 2

- N10 (readout N4 line from the last event, not `pre_news_until`): added to the spec,
  killed by `test_the_readout_n4_line_ends_with_the_pre_news_until_window`.
- Reviewer's N11 (malik `b.end < entry_ts` to `<=`, pre-existing code, not in this
  spec): equivalent in practice, as the reviewer argued. An entry at a window's last
  microsecond is inside the blackout (no entry is sent then, and a held position gets
  the NEWS exit anyway).
