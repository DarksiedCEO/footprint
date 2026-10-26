# Noise-band test on ES (noiseband-es-v1): pre-registration

Status: SIGNED by Andre (2026-10-23). Written and committed before any code for the rule
exists and before it has been run on ES data. The pre-registration below is copied
verbatim from the signed text. Nothing in it may change after a result is seen; a change
makes it a new hypothesis that counts against the luck bar.

ES data is used ONCE, after independent review of the build. Building and testing use
synthetic data only; NQ data may be used only for a signal-parity check against
tournament.py (no NQ P&L is read beyond what parity needs).

## Signed pre-registration (verbatim)

- Rule source: tournament.py's `noise_band(1.5, 15)` with first-signal-per-day (`claude/research/tournament.py` in the Claude project; a copy is at /tmp/claude-0/-home-claude/f2cf3b17-ea24-5ac3-a7fa-41e6238c74a7/scratchpad/research/tournament/tournament.py, sha256 4c0db0b7dcea5bb52fa2f5d33a9a9905ba29b894ac9c8f50404b536154a3069c). Read it fully; your implementation must reproduce its signals exactly (same RTH filter 09:30–16:00 ET weekdays, days with < 300 RTH bars skipped, 14-session warm-up, sigma per minute = mean over the previous 14 kept sessions of |close at the last bar at-or-before that minute ÷ that past day's first RTH open − 1|, bands top=max(today's first RTH open, previous kept day's last RTH close)×(1+1.5σ), bottom=min(...)×(1−1.5σ), signal checked only at closes of bars with session minute m ≥ 30 and (m+1) % 15 == 0, call above top / put below bottom, entry at the NEXT bar's open, first signal of the day only, flat by 15:55 ET (minute 385)).
- N1 exit: the harness's existing `booker` exit (1 contract), unchanged.
- N2 exit: tournament.py's `vol_stop25_tgt50` in futures points: stop 0.25 × ADR, target 0.50 × ADR, ADR = mean of (high − low) of the previous 14 kept RTH sessions; stop checked before target within a bar; else flat 15:55. 1 contract. Implement as a new exit kind in research exits, numbers in config.
- Pricing: harness Black-76 at the money, rescaled as the harness does; vol = prior day's VIX close (the existing load_vol works on Cboe CSVs); days to expiry 1 (2 as sensitivity via the existing CLI flag).
- Instrument: ES, point value 50, tick 0.25 → add config/instruments/es.yaml mirroring nq.yaml's schema with ES values (state every value and its source; anything unknown must be marked UNVERIFIED and must not affect the research run). reference price for rescaling: decide via config; document (the NQ run used 20000; for ES use a value fixed in config, e.g. ES's level at the time of signing ~7,700 — mark DEFAULT (draft) and say it in the doc).
- Costs: judged at $45 per contract round trip; $30 and $90 reported as sensitivity. Must be config (e.g. a per-instrument research cost or a CLI/config override) — no literals in src.
- Pass bar: existing judge (go-live gates from backtest.yaml + 2 of 3 thirds positive) AND t ≥ 2.3 (two scenarios tested). Add the t bar via config.

## Scenarios (judged once each)

Two scenarios: exit {N1 booker, N2 vol bracket}, days to expiry 1, cost 45. Reported as
sensitivity only (not judged): days to expiry 2, and costs 30 and 90.
