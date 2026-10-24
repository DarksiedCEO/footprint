# Noise-band ES test: implementation notes (2026-10-23)

Addendum, dated 2026-10-23, written after the signed pre-registration
(`noiseband-es-v1.md`, unchanged since its commit e9aef0b). NOT part of the
pre-registration: it changes no rule or number.

Branch noiseband-v1. Nothing here changes the signed text; it records how it was built
and where the harness differs from tournament.py. Decisions for Andre: NB-1 to NB-10 in
docs/decisions-pending.md.

- Rule: `research/rules/noise_band.py`, settings `config/research.yaml` `rules.noise_band`.
  Signal parity with tournament.py's `noise_band_x1.5_every15`, first signal per day, on
  the NQ raw continuous file (2010-2024): 1437 of 1437 signal days agree on date,
  direction and entry minute, same list in the same order, 0 mismatches
  (`qa/research/noiseband_parity.py`, output `qa/research/noiseband-nq-parity.out`).
  The tests also compare both on synthetic data (six seeds; gaps, short days, a
  Saturday, the March clock change) with an extract of the source in
  `tests/research/tournament_reference.py`.
- Entry: the engine's new `next_open` signal enters at the open of the bar after the
  signal bar, valued at that bar's open time; the entry bar's extremes and close are
  walked as path points (the source checks the entry bar too).
- N1: `--exit booker` (risk.yaml stop and lock, 1 contract), unchanged.
- N2: `--exit adr_bracket` (research.yaml `exits.adr_bracket`): stop entry -/+ 0.25 x
  range, target entry +/- 0.50 x range, in futures points, valued as the option at those
  futures prices at each path point; adverse extreme first, so a bar reaching both
  stops out; every contract sold at the first level reached.
- Flat (NB-1): the harness closes at the OPEN of the first bar opening at or after
  15:55 ET (its existing rule). tournament.py closes at the CLOSE of the bar at session
  minute 385 (one minute later). A level the price opens through fills at the open's
  value in the harness; tournament.py fills such a gap at the level itself.
- Keep rule (NB-6): a day is kept only with at least the configured number of regular
  bars. That count is known only at the day's end; inherited from the source rule and
  pre-registered. The report prints it with the day counts.
- Instrument (NB-2, NB-4, NB-5): `config/instruments/es.yaml` (symbol ES, point value
  50, tick 0.25; every other value copied from nq.yaml, UNVERIFIED for ES and unused by
  research). research.yaml `instruments.es.yaml`: reference price 7700 (DEFAULT, draft:
  about the ES level at signing), round trip 45 (signed), strike step 5 (DEFAULT, draft,
  UNVERIFIED: ES option strikes near the money). Corrected after review L-1: the step
  has no effect on this run, because with moneyness 0 the strike is the reference
  price, 7700, a multiple of the step (see NB-4).
- Costs (NB-3): the judged 45 is config; the 30 and 90 sensitivities use the CLI's new
  `--cost` (the report states the override).
- t bar: research.yaml `judging.min_t_stat.noise_band: 2.3`, applied by the CLI to this
  rule only (London keeps its pre-registered verdict, no t bar).

## Commands for the single ES run (after independent review; not run)

    B=/home/claude/data/es/es_raw.csv; V=/home/claude/data/VIX_History.csv
    R="uv run python -m footprint.research --bars $B --vol $V --rule noise_band --instrument es.yaml"
    # judged (2 scenarios)
    $R --exit booker
    $R --exit adr_bracket
    # sensitivity (reported, not judged)
    $R --exit booker --days-to-expiry 2;      $R --exit adr_bracket --days-to-expiry 2
    $R --exit booker --cost 30;               $R --exit adr_bracket --cost 30
    $R --exit booker --cost 90;               $R --exit adr_bracket --cost 90

## Review round 1 changes (dated 2026-10-23; no rule or number of the pre-registration changed)

- The signed t bar can no longer be dropped by a misspelt or missing config entry (M-1).
- The vol file must be the instrument's index: VIX for ES (`vol_index`, L-4).
- The rule notes list dropped weekdays with a mid-day gap (L-3), signals refused because
  the next bar opens at or after 15:55 (NB-11), and kept days ending before 15:55, not
  traded (NB-12). NB-11 and NB-12 are fail-closed refusals where the harness cannot
  follow the source; each is counted and dated in every report.
