# noiseband-v1: mutation notes

Spec `noiseband-v1.json` (70 mutants: the rule, the engine's next-open entry and
prepare hook, the adr_bracket exit and its levels, the per-instrument settings, the
t bar, the --cost flag). Evidence: `noiseband-v1.out`, run on the code commit with a
clean tree.

## Trial run (dirty tree, before the code commit): 70 mutants, 62 killed, 8 survived

| Mutant | Resolution |
|---|---|
| 32 first check exclusive (`<` -> `<=`) | Test added: `test_first_check_minute_is_inclusive` (first_check_minute 44, itself a checkpoint, must signal at 44). With the signed 30 the mutant is invisible (30 is not a checkpoint). |
| 35 top inclusive (`>` -> `>=`) | The test's hand-computed band differed from the rule's in the last float bit, so "close == top" was never exercised. The test now takes the band from `levels()` and sets the close exactly to it. |
| 39 put without range | Test added: `test_put_signal_carries_the_range_and_next_open`. |
| 40 report not-kept count | The old substring check `...: 1` also matched `...: 16`; the test now compares the whole line. |
| 41 entry bar open walked again / 42 entry open re-checked | The entry bar's open is the entry price, so walking it again cannot fill; it only adds a duplicate mark. Test pins the mark count of a next-open trade (`test_next_open_entry_values_the_next_bar_open`). |
| 45 next-open time = trigger close time | Equal on contiguous bars. Test added with a gap between the trigger bar and the next bar: `test_next_open_after_a_gap_enters_at_the_next_bar_time`. |
| 46 prepare not called | The bracket group's rules had no prepare. Test added: `test_run_calls_prepare_once_with_every_day`. |

No mutant was argued equivalent or dropped.

## Evidence run: 70 mutants, 70 killed, 0 survived or errored

On 36d452c with a clean tree (`noiseband-v1.out`, first line: tree-dirty 0 files).
