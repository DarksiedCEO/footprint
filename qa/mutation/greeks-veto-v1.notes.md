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
