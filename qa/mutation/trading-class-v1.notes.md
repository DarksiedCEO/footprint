# trading-class-v1 mutation notes

## First run (source 9c353b2, clean tree)

16 mutants: 15 killed, 1 survived: #3 `is_american` accepted a non-quote (its
TypeError guard had no test). Fixed in 303cd30. That commit's message names the
source as "50a2cb6"; that hash is wrong, the run was on 9c353b2.

## Second run, after review round 1 (source 18dc0f9, clean tree)

27 mutants (11 added for the European allowlist, the chooser and the model check):
24 killed, 3 survived, each a real test gap, fixed in 3122dc2:
- #16 the American exclusion inside `is_european_class` (the loader refuses an
  overlapping pattern, so only a hand-built profile reaches it; tested directly).
- #22 a string accepted as the pattern list (the test string "QN[1-5]" failed on
  "[" anyway; now "QNE", where every character is a valid pattern).
- #27 the model ignoring its configured class (the config value equalled the
  mutant's literal; now also tested with QN2).

## Third run (source 3122dc2, clean tree)

27 mutants: 27 killed.
