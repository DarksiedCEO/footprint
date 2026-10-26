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
