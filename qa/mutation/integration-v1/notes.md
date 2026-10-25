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
