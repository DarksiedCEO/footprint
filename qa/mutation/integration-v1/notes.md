# integration-v1: sibling mutation specs re-run on the integrated tree

Every `.out` here was run on f287f74 (after merges #42, #43, #44, #37, noiseband/#38) in a
clean clone of integration-v1 (`tree-dirty: 0 files` on each first line), with each
sibling's own spec unchanged: `uv run python qa/mutation/mutate.py qa/mutation/<spec>.json`.

| Spec | Result here | Branch's own last evidence | Non-kills and why |
|---|---|---|---|
| decay-exit-v1 | 51, 3 errored | 51, 5 survived (later killed) | BAD 35, 36, 40: their text is not in malik.py at #42's own tip either (malik.py here is byte-identical to origin/decay-exit-v1); superseded by decay-exit-v1-round2. The 5 round-1 survivors are killed. |
| decay-exit-v1-survivors | 5, 0 | 5, 0 | none |
| decay-exit-v1-round2 | 78, 0 | 78, 0 | none |
| greeks-veto-v1 | 61, 0 | 61, 0 | none |
| greeks-veto-v1-r1 | 41, 3 errored | 41, 0 (on 66d1db5) | BAD 39, 40, 41: config.py's gamma bound was rewritten in #43's own round 2 (R2-4); absent at #43's tip too; re-targeted by greeks-veto-v1-r2. |
| greeks-veto-v1-r2 | 19, 3 survived | 19, 3 survived | 1, 2, 17: the same three #43 argued equivalent (greeks-veto-v1.notes.md). |
| playcaller-v1 | 64, 0 | 64, 0 | none |
| playcaller-wire-v1 | 84, 1 errored | 84, 0 | BAD 40: integration-caused. After #43 the pipeline passes the play to Decision by keyword (`play=play,`). Re-targeted as `integration-v1-retarget.json` (same mutation, `play=None`): KILLED. |
| noiseband-v1 | 70, 2 | 70, 2 (rerun on 08d0ede) | 30 SURVIVED, 31 BAD: the same two noiseband-v1-r1.notes.md argues. |
| noiseband-v1-r1 | 28, 0 | 28, 0 | none |
| london-v1 | 35, 0 | 35, 0 | none |
| research-v1 | 80, 5 errored | 80, 0 (on 8f30bc2, research-v1's own tree) | BAD 30 (2 matches), 35, 37, 43, 66: noiseband-v1 rewrote these lines in research/exits.py, engine.py and __main__.py; the files here are byte-identical to origin/noiseband-v1, so the same 5 are BAD at noiseband's tip. Not caused by integration; research-v1.json was never re-targeted after noiseband (its own spec covers the new code). Open for the noiseband reviewer. |

No mutant that was killed on its own branch survives here.

## After merging #45 (cap10k-v1) and #47 (backtest-history-v1): on fb7dc7c

In `on-fb7dc7c/`, clean clone (`tree-dirty: 0 files`), specs unchanged.

| Spec | Result here | Branch's own last evidence | Non-kills and why |
|---|---|---|---|
| backtest-history-v1 | 63, 0 | 63, 0 (on 1ce3730) | none |
| backtest-history-v1-r1 | 32, 0 | 32, 0 (on 5ea7083) | none |
| backtest-history-v1-r2 | 12, 1 | 12, 1 (on 5ea7083) | 9 SURVIVED: the one #47 argued equivalent (backtest-history-v1-r2.notes.md). |
| backtest-history-v1-r3 | 2, 0 | 2, 0 | none |
| cap10k-v1 | 5, 3 errored | 5, 0 (on 397aaec, cap 10000) | BAD 1-3: `position_cap: 10000` is not in risk.yaml at #45's own tip (6000 since b475202); superseded by cap10k-v1-r2. |
| cap10k-v1-r1 | 32, 7 | 32, 3 (on 1e70c69, cap 10000) | BAD 1, 2, 3, 11, 22, 23: same stale 10000 text; 7 SURVIVED: #45's argued equivalent (cap10k-v1.notes.md). |
| cap10k-v1-r2 | 38, 3 | 38, 3 (on b475202) | 1, 3, 7: the same three #45 argued (cap10k-v1.notes.md). |
| decay-exit-v1-round2 | 78, 1 errored | 78, 0 | BAD 76: integration-caused (#45's position_cap line now sits between stop_loss and the decay lines). Re-targeted both ways in `integration-v1-retarget-2.json`: 2 of 2 KILLED. Re-run because two #42 tests were adapted in the #45 and #47 merges. |

No mutant killed on its own branch survives after integration.

## Review round 1 fixes: on 027f47c

Both specs were run in this worktree on a clean tree (`tree-dirty: 0 files`).

| Spec | Result | Non-kills and why |
|---|---|---|
| integration-v1-retarget-3 (C1) | 1, 0 | none. This is greeks-veto-v1-r2 #4, which went BAD after #47 rewrote its line to `check_model_vol(..., self._bars[self._first :], reference)`. It is re-targeted here as the reviewer's R25 (`self._first + 1 :`), and test_model_vol_guard.py kills it. |
| integration-v1-round1 (L1, L2) | 13, 1 | 5 SURVIVED: X5 is a no-op control (`* 1` on a Decimal), equivalent by construction. |

What the round-1 spec covers:
- **X1–X4, tests/backtest/test_integration_cross.py.** It catches each of these: the roll gate dropped from the combined engine, the roll console never blocking, the live exit losing the decay exit, and option-model rescaling removed.
- **E1–E8, tests/test_es_profile_refused.py.** It catches each of these: the loader's stop_max check removed (es.yaml then loads), and each of the six trading entry points ignoring `--instrument` (live, check, kill_switch, backtest, history, record_chain). It also catches the live runner carrying on after a refusal.

With this, greeks-veto-v1-r2 has no unaccounted BAD mutant on the integrated tree. Its three survivors (1, 2, 17) are the ones #43 argued equivalent.
