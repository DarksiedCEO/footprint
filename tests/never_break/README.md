# Never-break tests

Adversarial tests for the never-break rules Andre approved on 2026-10-04
(doc: "Footprint — Never-Break Tests & Money Model"). If any fails, nothing ships.
Each rule gets its test in the PR that builds the component that owns it.

| ID | Rule | Owner | Status |
| --- | --- | --- | --- |
| NB-01 | Stop can never be widened | stop (Booker) | pending component |
| NB-02 | Resting hard stop at the broker on every entry | execution (Diego) | pending component |
| NB-03 | No order without risk-layer approval | execution (Diego) | pending component |
| NB-04 | No AI agent touches sizing, stops or execution | core models | partial: `Read` carries no order fields (tests/test_models.py) |
| NB-05 | Never add to a loser (one position at a time) | risk (Dolores) | **tested** |
| NB-06 | No entry in a news blackout | risk + session (Cyrus) | **tested** |
| NB-07 | Overnight flat by the overnight session's end | session (Cyrus) | **tested** (rule); enforcement pending trade mgmt |
| NB-08 | Two stop-outs ends the day | risk | **tested** |
| NB-09 | Daily loss limit | risk | **tested** |
| NB-10 | Weekly loss limit | risk | **tested** |
| NB-11 | Kill switch on drawdown, latched until Andre clears it | risk | **tested** |
| NB-12 | Limit orders only on options | execution (Diego) | pending component |
| NB-13 | Trade-count ceilings (v1: 0 overnight) | risk | **tested** |
| NB-14 | No countertrend entries | console (Desmond) | pending component |
| NB-15 | VWAP side rule | console / levels (Grant) | pending component |
| NB-16 | Flatten on unclear recovery | trade mgmt (Malik) | pending component |
| NB-17 | Position cost within the cap | risk | **tested** |
