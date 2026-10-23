# greeks-veto-v1: mutants argued equivalent

Round 2 review (reviewer's mutants 1 and 2, kept in greeks-veto-v1-r2.json, where they
still survive). Not proven; argued.

## Vega band edges in the backtest guard (`backtest/model_check.py`, `_value`)

- `vol < float(s.vega.min_implied_vol)` changed to `<=`
- `vol > float(s.vega.max_implied_vol)` changed to `>=`

Each changes the verdict only when the solved volatility is exactly equal to the bound
as a float. The volatility is the bisection result of `core/implied_vol.py` on a model
mid that lies on the option tick (0.25 points), run "to the last float"; the bounds are
decimal settings (0.08, 1.0) converted to binary floats. For the solved value to land
bit-for-bit on that float, the Black-76 price at exactly the bound would have to equal a
tick-rounded mid to the last bit, which the round-1 and round-2 sweeps never met (none
of the thousands of prices checked, at quarter-point steps across each edge, produced a
tie). And at such a tie the two readings agree in substance: the Vega agent itself
treats its band as inclusive (it solves inside [min, max]), which is what the code
does; the mutant would make the guard refuse one price the agent allows, failing
closed. No test can produce the exact tie without forging a quote off the model, so
the mutants are recorded as equivalent in practice.

## Gamma loader bound: one side left in local time (`core/config.py`, `_check_gamma_reachable`)

Round 2 own mutant 17 ("no UTC: clock change ignored"): drops `.astimezone(UTC)` from
`ends` only, leaving `opens` in UTC. Equivalent: Python subtracts two aware datetimes
with *different* tzinfo objects by first converting both to UTC, so `ends - opens` is
the same real elapsed time either way. The conversion matters only when both sides keep
the *same* ZoneInfo object (then the subtraction is naive and the clock-change hour is
lost: 81 hours instead of 82). That case is pinned by the tests: dropping UTC from both
sides gives 81 hours and test_a_gamma_minimum_under_the_longest_possible_wait_loads[82-3]
fails (seen while building: the first, naive calculation gave 81). The mutation run
evidence for that two-sided variant is not in the .out; it was observed by hand only.
