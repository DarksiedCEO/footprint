# Mutation notes: playcaller-wire-v1

Spec: `qa/mutation/playcaller-wire-v1.json` (54 mutants over decision/play.py, the
pipeline, the journal, the backtest engine and readout, config, the live build and
program).

## Round 1 (code f4e08b6, evidence f3bddf9): 51 of 54 killed

Survivors, each now tested (tests/decision/test_play.py, "mutation survivors"):

- #27 `back < calendar_from` -> `<=` in `warm_up`: the repo's calendar_from (2026-01-01)
  is a holiday, so no history day ever equals it. Killed by
  `test_history_reaching_exactly_calendar_from_is_not_warm_up` (calendar_from moved to a
  trading day; the reader labels that day, so the play caller must too).
- #29 the need missing the oldest NIGHT, and #33 the night opening on its own trading
  day: with the repo lookbacks (60 openings, 20 nights) the oldest bar read is always an
  opening, so the night terms never decide the need. Killed by
  `test_the_need_matches_the_readers_reading_list` (lookbacks with the overnight one
  longer, against a hand count of the reader's reading list).

A re-check of only these three on the test commit killed all three; the final evidence
is the whole spec on the test commit (`playcaller-wire-v1.out`).

## The cherry-picked play caller (PR 39) re-run here

`qa/mutation/playcaller-v1.json` (PR 39's spec, unchanged) re-run on f4e08b6 after the
cherry-pick onto trading-class-v1 and this wiring: source f4e08b6, tree-dirty 0, baseline
passed, **64 mutants, 0 survived or errored**. PR 39's own `playcaller-v1.out` is left
as it was (its evidence for 85d3663); the only change to regime_reader.py here is one
docstring line (which play runs on which label now names decision/play.py).

## Review round 1 fixes (code d141ffa)

30 new mutants (R1-*, #55-#84) for M1 (waiting for the opening's final minute, the
window-end NO_DATA, the closed-bar test), M2 (the bad history day on the reader's call,
the once-a-day alarm), L1/L4 (the load-time checks, PlayRefused, the program's REFUSED
exits), L2 (the stand-in's clock) and L3 (the journal's owed play record). Mutant #54's
text follows the rename of its handler to `PlayRefused`.

Evidence on d141ffa, tree-dirty 0, every baseline passed:
- `playcaller-wire-v1.out`: **84 mutants, 0 survived or errored** (no TIMEOUT).
- `playcaller-v1.wire-r1.out`: PR 39's spec (`playcaller-v1.json`, unchanged) re-run
  because regime_reader.py changed (bad_history_day): **64 mutants, 0 survived or
  errored**. This also closes review round 1 I4 (that spec's evidence is now bound to a
  commit of this branch; `playcaller-v1.out` stays as PR 39's evidence for 85d3663).

No survivors, so no equivalence arguments.
