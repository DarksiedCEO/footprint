# cap10k-v1 mutation notes

## Original spec (qa/mutation/cap10k-v1.json, source 397aaec, clean tree)

5 mutants: 5 killed. The reviewer reproduced this on cd17e4d.

## Reviewer's extra mutants (round 1, cap10k-extra.json, 10 mutants)

6 killed, 4 survived. All 10 are carried into qa/mutation/cap10k-v1-r1.json as R1-R10.

## Round-1 spec, first run (source e8e1419, clean tree)

32 mutants: 28 killed, 4 survived.

- **R1 `position_cap: 9998` (sizing group): equivalent at test resolution.** Ezra's cost
  per contract is `ceiling x 20 + 2.50` with the ceiling on the 0.25 tick, so costs move in
  $5 steps ending in 2.50 or 7.50. Every cap in [9,997.50, 10,002.50) admits and refuses
  exactly the same contracts: 9,998 and 10,000 are indistinguishable by any sizing outcome.
  The shipped value itself is pinned by `tests/test_config.py` (the config group kills
  any change of it, e.g. #11, and the original spec's 9997/10003 boundary mutants).
- **R3 `position_cap: 20000` (risk/decision group): survives by design.** Those tests are
  written relative to `risk.position_cap` (they test that the gate holds whatever the cap
  is), so they carry no opinion on its value. The value is pinned in the config group;
  #2 (the same mutant in the sizing group) is killed.
- **R7 `int(cap // per_contract)` -> `int(cap / per_contract)`: equivalent.** Both
  operands are positive Decimals (the loader requires a positive cap; per_contract is a
  ceiling of at least one tick times a positive point value plus a non-negative fee). For positive
  values `//` truncates toward zero and `int()` of the true quotient also truncates toward
  zero, so the results are identical for every reachable input.
- **#24 Felix takes the farthest expiry (fridays group): a real gap in the new test,
  fixed.** On the Thursday and Friday modelled chains only one expiry lies inside 1-3
  days, so nearest and farthest coincide. The test now also runs a Monday, where the
  model lists 1-, 2- and 3-day expiries and Felix must take the 1-day one (the mutant
  then picks 3 days and fails). Felix's nearest-expiry rule was already pinned in
  tests/agents/test_contract_sizing_stop.py; this closes it for the backtest-level test.

R10 (`cap > bankroll` -> `>=`, a pre-existing gap the reviewer found) is now killed by
`test_risk_accepts_a_position_cap_equal_to_the_bankroll`.

## Round-1 spec, second run (source 1e70c69, clean tree)

32 mutants: 29 killed, 3 survived: R1, R3 and R7, each argued equivalent or
by-design above. #24 is now killed. Evidence: qa/mutation/cap10k-v1-r1.out.
