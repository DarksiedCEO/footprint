# PR 38 (london-v1): mutation notes

Evidence: `london-v1.out` (spec `london-v1.json`), run on the PR's own commit.

## Trial run (before the commit): 31 mutants, 28 killed, 3 survived

| Mutant | Resolution |
|---|---|
| 9 entry start ignored | Test added: `test_entry_start_is_read_even_when_it_is_later_than_the_range_end` (entry_start moved to 03:20 in the settings object; 03:15-03:19 closes outside the range do not enter). With the pre-registered config, entry_start equals range_end, so the range check alone already blocked earlier bars. Killed in the evidence run. |
| 1 range may include the trigger bar (`range(i)` -> `range(i + 1)`) | Argued equivalent, dropped from the spec. The rule returns before reading the range unless bar i opens in [entry_start, entry_end), and the loader refuses entry_start < range_end. So bar i never has a clock in [range_start, range_end) and is never in the range, with or without the `+ 1`. |
| 4 range reads the whole day (`range(i)` -> `range(len(day))`) | Argued equivalent, dropped from the spec. A session day runs from the roll (18:00) to the next roll, so its wall clocks rise after midnight and each of the 03:00-03:14 minutes occurs at most once. The loader orders range_start < range_end <= entry_start, and bar i opens at or after entry_start, so every bar after i opens after range_end and none of them is in the range. The no-lookahead property itself is pinned by `test_no_lookahead_the_answer_at_a_bar_ignores_every_later_bar` (each answer equals the answer on the day cut after that bar) and `test_no_lookahead_after_a_cached_range`. `range(i)` is kept so the rule never reads a later bar even if a future config breaks that ordering (which the loader refuses today). Caveat, not verified on data: a daylight-saving fall-back repeats 01:00-01:59 New York, not 03:00-03:14, so it does not reach the range window. |

The evidence run therefore has 29 mutants.

Result: 29 killed, 0 survived or errored, on c363d9f with a clean tree.

## Review round 1 (a2edcbb, CERTIFIED WITH CONDITIONS)

- 38-M1: the reviewer's surviving mutant `close > high` -> `day.high[i] > high` is in the
  spec, with its put-side twin; killed by
  `test_a_wick_above_the_range_that_closes_inside_is_no_entry`.
- 38-M2: the rule reports the sessions it could not trade for want of a full range,
  per year (`LondonOrb.report`, printed by the CLI under "rule notes"); mutants on the
  count, the per-year lines and the printing are in the spec.
- 38-I1: the clock-change caveat is in the same notes.
- PR 37's fixes (18491d5) were cherry-picked here as 68867c7 (one import conflict in
  tests/research/test_cli_settings_isolation.py, resolved by keeping both sides).

Evidence after review round 1: 35 mutants, 35 killed, 0 survived or errored, on 28eac4b with a clean tree. The research-v1 spec (78 mutants) was also re-run on 28eac4b: 78 killed.

Evidence after review round 2: PR 37's round 2 fix cherry-picked here as 06c1014 (gate: ruff clean, 249 files formatted, mypy clean on 233 files, pytest 2459 passed, 17 skipped). On 06c1014 with a clean tree: london-v1 spec 35 of 35 killed; research-v1 spec 80 of 80 killed.
