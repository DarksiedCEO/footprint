# D41 (b) mutation notes

Spec: `qa/mutation/d41-stop-alarm-v1.json` (34 mutants; group d41 runs
test_d41_stop_wait_alarm.py, test_pending_entry_stop.py, test_partly_filled_stop.py,
test_grind_g7_3.py).

- Run 1, on c0ade33 (`d41-stop-alarm-v1.run1.out`): 32 killed, 2 survived.
  - 12 (the `self._alarm_sink is None` check removed): calling None raises TypeError,
    which is caught and logged "could not deliver an alarm". Killed in 520aaa8: the
    unrouted test asserts no failed-delivery log.
  - 29 (a FILLED stop no longer returns early): it fell into the G6-1 partly-sold halt.
    Killed in 520aaa8 by `test_unit_a_stop_reported_filled_gets_no_alarm_no_halt_no_replacement`.
- Run 2, on 520aaa8, clean tree (`d41-stop-alarm-v1.out`): 34 of 34 killed, 0 survived,
  0 errors. No equivalence arguments needed.

## PR #53 review round 1 (NOT CERTIFIED) and the fixes in d571023

- The reviewer's own mutants R1, R2, R3, R4, R7 and R8 are added to the spec, adapted to the
  new text (R5 and R6 duplicate mutants 2 and 10). The group now also runs
  tests/execution/test_runner.py.
- Mutants on the old `outcome` expression were replaced by mutants on the same behaviour
  in `_replacement_outcome` and the unknown-outcome branch. Mutants on the new F2 wording
  were added for each replacement state: filled, held back / being cancelled, partly sold,
  ended, unknown.
- Run 3, on d571023 with a clean tree (`d41-stop-alarm-v1.out`): 53 mutants, 53 killed,
  0 survived, 0 errors. No equivalence arguments.

## PR #53 review round 2 (CERTIFIED WITH CONDITIONS): N1-N5

The reviewer's N1-N5 are added to the spec (58 mutants). Two new tests kill N5 and N2:
- `test_a_rejected_replacement_that_sold_part_gets_the_partly_sold_wording` (N5);
- `test_a_live_held_back_replacement_that_sold_part_is_still_worded_live` (N2).

The other three are argued below, as the reviewer described them. They SURVIVE in
`d41-stop-alarm-v1.out` by design.

- **N3** (`"held back" if placed.held_back else "being cancelled"` keyed on `cancelling`
  instead): equivalent by construction. The WORKING branch of `_replacement_outcome` is
  reached only when the stop is WORKING and does not protect, so `held_back` or
  `cancelling` is true. The IBKR adapter sets `cancelling = unable and not held_back`
  (adapter.py, reviewer cites lines 924-925), so the two flags are never both true. With
  exactly one true, both conditions pick the same word.
- **N4** (`sold {placed.filled}` -> `sold {placed.order.quantity}` on FILLED): **the equivalence argued here in round 2 was WRONG** (review round 3, R3-F1). The IBKR adapter can answer FILLED with filled 0 when the open-order message arrives before the status message (adapter.py:600-607): `place()` returns on the first update. The alarm then read "sold 0". Fixed in the source (alarm text only): with filled 0 the count is omitted, and the alarm still says "the position is sold; do not place a stop by hand". `test_a_replacement_reported_filled_before_its_count_never_says_sold_0` kills the new mutant "`> 0` -> `>= 0`" (which would show "sold 0"). It does NOT kill N4: on 65fa788 N4 still survived (run 5, kept as `d41-stop-alarm-v1.run5.out`), because with a count above 0 every test had count == quantity. `test_a_filled_replacement_names_the_count_the_broker_reported` (FILLED reported with 1 of 2 counted) kills it: the alarm names the broker's count, never the order's size.
- **N1** (`placed.protects` -> `placed.protects and placed.filled == 0`): unreachable at
  `max_contracts: 1` (config/risk.yaml; above 1 is refused). With quantity 1, any fill
  makes the stop FILLED, so a protecting (WORKING) replacement always has filled == 0.
  Under the mutant, a protecting replacement that sold part would read "live and being
  cancelled ... do not place another stop beside it", which still forbids a hand stop.
  **Before scaling past 1 contract:** add a test for a partly filled replacement that
  still protects, together with a wording decision. Today it reads "placed" and omits
  the partial sale (finding R2-F2, INFO). This is recorded in the D41 row.
- N2 is also unreachable at 1 contract. It is killed anyway by the test above, which
  pins the live wording for scaling.
- Run 4, on beb7f2e with a clean tree (`d41-stop-alarm-v1.out`): 58 mutants, 55 killed,
  3 survived (54 N1, 56 N3, 57 N4, each argued above), 0 errors.

## PR #53 review round 3 (CERTIFIED WITH CONDITIONS, R3-F1)

- The N4 note above is corrected and N4 is now killed. The spec gets one new mutant ("sold 0" shown when the count lags). The mutants on the FILLED wording are re-targeted to the new text: "the position is sold" dropped, the count dropped, the FILLED branch removed, N4. The spec now has 59 mutants.
- Run 6, on 36e217c with a clean tree (`d41-stop-alarm-v1.out`): 59 mutants, 57 killed,
  2 survived (54 N1, unreachable at 1 contract and needing a test before scaling; 56 N3,
  equivalent by construction), 0 errors.
