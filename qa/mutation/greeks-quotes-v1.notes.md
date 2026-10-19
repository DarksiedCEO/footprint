# PR 35 (greeks-quotes-v1): mutation notes

Evidence: `greeks-quotes-v1.out` (spec `greeks-quotes-v1.json`), re-run on the PR's own
commit after the review fixes.

## Reviewer mutants (independent review of 17d7f5b) and what became of them

| Reviewer id | Mutant | Now |
|---|---|---|
| P2 | the "computed" check reduced to implied volatility only | Gone with the code: a reading is now judged by `judge_greeks` (NOT_COMPUTED / INCOMPLETE / IMPOSSIBLE / USABLE); incomplete readings are their own verdict and are pinned per field (`test_one_value_missing_makes_the_whole_reading_incomplete`, `test_one_value_alone_is_incomplete`) |
| P4 | the all-None check skips the underlying price | Gone with the code (same reason); a reading with only the underlying price is pinned as INCOMPLETE |
| P6 | `to_greek` reads a real 0 as missing | In the spec ("a real 0 read as missing"), killed by `test_zeros_arrive_as_zeros_not_as_missing` |
| P8 | reading stamped one second early | Killed (reviewer) by the age-boundary test |

## Argued equivalent (kept out of the spec)

- **P1 / P7: an unknown right read as a call, or not counted.** `on_greeks` treats a line
  whose contract right is not C or P as unusable. No such line can exist: option lines
  are made only from contracts `_take_expiry` kept (`c.right in _RIGHTS`), and held
  lines are IBKR options (C or P). The branch is defensive; a mutant there changes
  nothing observable through any reachable path.
- **P3: `option_greeks` without the connection check.** On a loss `_new_session`
  empties `_lines`, and lines are only re-added by `step()`, which does nothing while
  disconnected; with no line, `option_greeks` returns None either way. The check stays
  as a second guard.
- **P5: the future's line cleared instead of returning.** The future's line never holds
  Greeks (`on_greeks` returns before storing for it), so clearing it changes nothing.
