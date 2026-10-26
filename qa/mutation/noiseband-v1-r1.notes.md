# noiseband-v1 review round 1: mutation notes

Spec `noiseband-v1-r1.json` (28 mutants on the round-1 fixes: M-1 t-bar checks, L-3
report, L-4 vol index, NB-11, NB-12). The branch spec `noiseband-v1.json` (70 mutants)
is re-run on the same commit. Evidence: `noiseband-v1-r1.out`, `noiseband-v1.out`.

## Trial run (dirty tree, before the code commit): 28 mutants, 27 killed, 1 survived

| Mutant | Resolution |
|---|---|
| 13 NB-12 report counts warm-up days | Test added: `test_nb12_counts_only_days_that_could_trade` (an early-ending kept day still warming up is not counted). |

## Equivalent mutants noted by the reviewer (L-2), not in the spec

- R10 (entry bar flat-checked too): the engine refuses `entry_ts >= flat_by` before the
  walk, and the entry bar opens at `entry_ts`, so that guard cannot fire. Now also the
  rule refuses such a signal (NB-11).
- R11 (`_target_taken = kind == "booker"`): never read for adr_bracket (`_bracket`
  returns first).
- R14 (`futures_level >= level`): a tie fills at the same value; only the label changes,
  and no rule here gives a futures stop with adr_bracket.
## Branch spec re-run on 08d0ede: 70 mutants, 68 killed, 1 survived, 1 bad

`noiseband-v1-rerun.out` (the round-0 evidence `noiseband-v1.out`, bound to 36d452c,
is kept unchanged). Both are made unreachable by the round-1 fail-closed rules:

- 30 "last regular bar may signal" (`n + 1 >= len(positions)` -> `n >= len(...)`):
  SURVIVED, argued equivalent. NB-12 leaves only kept days whose last regular bar
  opens at or after the flat time, and a signal bar must open before it, so the signal
  bar is never the last regular bar. The check stays as a guard against an index past
  the day's end.
- 31 "flat bar may signal" (`clock[i] >= flat` -> `>`): BAD MUTANT (its text moved
  onto the line above in round 1). The mutated form is also equivalent now: a signal
  bar opening AT the flat time has its next bar opening after it, so NB-11 refuses the
  signal anyway (`test_no_signal_from_the_flat_time_on` and the NB-11 tests pin the
  outcome). The check stays as the source rule's own cut-off.

## Evidence run: 28 mutants, 28 killed, 0 survived or errored

On 08d0ede with a clean tree (`noiseband-v1-r1.out`).
