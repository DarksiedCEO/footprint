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
