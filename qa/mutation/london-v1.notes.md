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
