# PR review guide: the Footprint stack in merge order

Every Footprint PR is stacked on the one below it. Review and merge **bottom up**:
#1 first, then retarget the next PR to `main`, and so on. Nothing merges without your
review. Claude never merges or force-pushes.

How to read the verdicts: **CERTIFIED / CONDITIONAL / NOT_CERTIFIED** are what each
PR's own body records from its hostile reviews (a separate reviewer session, same model
family). A same-model review is not a human review: **your review is the gate.** Since
2026-10-10 the grind reviewers give at most CONDITIONAL. This guide was written from
the PR bodies and the review reports on PR #21; it did not re-run any of those reviews.

Merge order: #1 → #2 → … → #21 (the base of everything new), then the draft data-day
PRs #22 → #23 → #24, then the Q5 backtest-rule PRs #25 → … → #31, then the Q6
comparison runner #32.

## Core system (#1–#10)

**#1 `scaffold` → main: Scaffold Footprint skeleton.** The repo layout, typed config
loaders, models, CI (actions pinned to commit SHAs) and the guard tests. No trading
logic. Look at: the inclusive session-end rule and that CI runs the four checks.

**#2 `never-break-risk-session`: risk gate, session guard, first never-break tests.**
Dolores (the veto on every entry, kill-switch latch, never clears it) and Cyrus
(prime/overnight/closed by Pacific clock, holidays from config). Two rounds of hostile
review, findings fixed. Look at: that the gate derives session and blackout from the
entry's own timestamp, and the money model (one contract, `max_trades_overnight: 0`).

**#3 `readers-v1`: trend, volume, momentum, range readers.** Mateo, Rafael, Darius
(the momentum entry: spike candle then follow-through) and Nadia. The PR body records
**no independent review of this PR on its own** (whether a later whole-tree audit
covered it is not verified here). Look at: the spike multiple (3x) and the
follow-through definition; these are the exact entry the Q5 "breakout" baseline keeps.

**#4 `levels-zeroday`: levels (Grant), zero-day (Nia), closed-bar aggregation.**
NOT_CERTIFIED → CONDITIONAL → **CERTIFIED** (round 3). Look at: decisions D1–D6, and
that round numbers are not walls.

**#5 `contract-sizing-stop`: contract choice (Felix), sizing (Ezra), stop and trail
(Booker).** **CERTIFIED** (round 4). Look at: 1–3 DTE, never falls back to another
contract, the position cap (now $10,000, CAP-1) held at the worst allowed fill, the $750 hard stop.

**#6 `console-pipeline`: console (Desmond) and the end-to-end entry pipeline.**
**CERTIFIED** (round 3). Look at: the hard gates in order, and that only an
`ApprovedEntry` this pipeline issued can reach execution.

**#7 `execution-v1`: execution (Diego), trade management (Malik), ledger and journal.**
Three NOT_CERTIFIED rounds (paths to a position with no stop, selling twice, going
short), all fixed; round 4 **CONDITIONAL**, conditions carried to the runner. Look at:
limit and stop-limit only (no market order type exists), the stop placed before the
ledger write.

**#8 `runner-v1`: the clock loop, one shared quote book, fail-safe flattening.**
**CERTIFIED** after 8 rounds. Look at: every start is a restart (reconcile first, no
entry until `clear <name>`), the 30 s unmanaged failsafe.

**#9 `order-path-v1`: one order record, safe resends, settled reconcile.**
**CERTIFIED** (5 rounds). Needs your written sign-off on **L1 and L2** (in
`docs/decisions-pending.md`).

**#10 `backtest-v1`: the real system on history, the G4 option model, the readout.**
**CERTIFIED** (3 rounds). Look at: the option prices are MODELLED (D22, every number
UNVERIFIED and printed in each readout); adverse-first inside each minute.

## IBKR and live (#11–#21)

**#11 `ibkr-v1`: IBKR order adapter, tested against a fake IBKR.** **CERTIFIED** at
479a34e (4 rounds). Nothing sent before
a full resync, no guessed positions, one order per id, stop and exits in one OCA group.

**#12 `ibkr-binding-v1`: official ibapi 10.50 binding and the read-only first look**
(`python -m footprint.brokers.ibkr.check`). **CERTIFIED** at c0c02cf (4 rounds). Paper only, one account, one instrument,
exact prices. Look at: that your stock is only ever "open elsewhere".

**#13 `restart-memory-v1`: a restart never decides the same minute twice.**
**CERTIFIED** at 6ee5ec1.

**#14 `market-data-v1`: market data connection** (contracts, chains, quotes, bars).
**CERTIFIED** at 5109cab (4 rounds).
Real-time only; a market data fault never touches orders.

**#15 `market-service-v1`: market data service** (front future, spliced back-adjusted
history, polled bars, option chain). **CERTIFIED** at 6a379d9 (2 rounds). Look at: the roll 8 days before expiry and the
100-point / 80-line chain limits (config defaults).

**#16 `session-backoff-v1`: reconnects escalate once, then back off.** **CERTIFIED**
at 6982b3d.

**#17 `live-startup-v1`: the live start-up program** (`python -m footprint.live`).
**CERTIFIED** at 12e641c (4 rounds). Paper
only, no entry until `clear <name>`, `--new-ledger` only on the first run, `quit` warns
if anything is held.

**#18 `resync-realarm-v1`: a stuck resync is alarmed again, also while flat.**
**CERTIFIED** at 4edc489. Decision D36 (the 300 s repeat).

**#19 `holidays-v1`: exchange calendar, flatten 5 min before an early close (D34).**
**CERTIFIED** at cac05a8 (2 rounds).
Look at: the holiday dates are **UNVERIFIED** against CME's own 2026/27 pages, and the
calendar ends 2027-03-31 (alarm 30 days before).

**#20 `alarm-channels-v1`: phone alarms (D20).** **CERTIFIED** at 22e1099. Telegram, SMS fallback, calls for
critical alarms; no credential ever in a log line.

**#21 `remediation-v1`: remediation of the AEGIS findings.** The hard-coded paper
boundary (DU accounts only; ports 4001/7496 refused), alarm isolation, disconnect,
fail-closed calendar, degraded alerting mode, the durable kill switch command, and the
two operator runbooks (`docs/runbook-dry-run-monday.md`; the data-day one is on #24).
Read the PR's FREEZE note and the latest audit comments on it. It is also where every
grind START/END and review report is posted. Look at: `brokers/ibkr/paper.py`, and the
dry-run runbook before Monday.

## Data-day prep (drafts, #22–#24)

**#22 `market-probe-v1` (draft): Q1 market-data probe**
(`check --market-data`). Read-only: the same `ReadOnlyGateway` refuses place, cancel
and global cancel. Prints the future's bid/ask and the nearest option quotes, or names
what is missing (IBKR 354 / 10167) and exits 1. Grind reviews: CONDITIONAL, every
CRITICAL/HIGH/MEDIUM and src LOW fixed (G27, G28). Look at: the two config defaults
(`probe_wait_seconds`, `probe_option_quotes`). UNVERIFIED against the real Gateway
until 2026-10-14.

**#23 `history-export-v1` (draft): Q2 history exporter**
(`history --out F [--days N]`). Writes exactly the CSV the backtest reads (round trip
tested), never overwrites, writes `.partial` first, refuses a gap longer than
`export_max_missing_minutes`. Grind reviews: CONDITIONAL, findings fixed (G27, G29,
G30). Look at: decision Q2-a (the 5-minute gap allowance) and Q2-b (gaps judged on
Footprint's own calendar).

**#24 `chain-recorder-v1` (draft): Q3 option-chain recorder**
(`record_chain --out data/chain/ [--minutes N]`). All three slices are built (the PR
title and body still say "slice 1 of 3"; they predate slices 2 and 3). One CSV per UTC
day; only fresh quotes (no older than the live `quotes.max_age_seconds`); a disconnect
writes nothing and the run goes on. Also holds `docs/runbook-data-day.md`. Grind
reviews: CONDITIONAL, findings fixed (G30–G34). Look at: open question **Q3-c** (should
the recorder keep a longer quote-age limit than the live 5 s?) and the defaults Q3-d.

## Q5 backtest rule library (drafts, #25–#31)

Andre's 2026-10-09 test queue, built from the definitions he approved on 2026-10-10
(word for word in `docs/strategy-rules.md`, on #27 and above). Every Q5 piece is
**backtest-only**: `footprint.backtest.rules` and `footprint.backtest.exits` are imported
by nothing outside `footprint.backtest` (a test enforces this), the live entry and exit do
not change, and with no `--entry`/`--exit` the backtest's output is byte-identical to
before (tested). Every candidate entry still passes the same gates (trend agreement,
session, risk gate, kill switch, levels, sizing, hard stop); every candidate exit is asked
**last**, after the hard stop and every existing reason to get out, and can only add a way
out. Synthetic test bars say **nothing** about profitability: real-data comparison runs
come after 2026-10-14 and are your call. Every reading made after your 21:12 PT approval
is in `docs/decisions-pending.md` marked "for Andre".

**#25 `rules-seam-v1` (draft): Q5.0 rules seam.** `python -m footprint.backtest --bars F
--entry <name> --exit <name>`. Unknown names are refused before anything runs. Grind
reviews: CONDITIONAL (G37, G38). Look at: the byte-identical no-option test.

**#26 `entry-breakout-v1` (draft): Q5.1 "breakout", the BASELINE.** The existing
momentum entry under a name; no logic changed. Grind reviews: CONDITIONAL (G38, G44).
Look at: reading Q5.1-b (you approved keeping the follow-through exactly as coded).

**#27 `entry-stairstep-v1` (draft): Q5.2 "stairstep" and "stairstep_vol".** Three valid
steps (higher pause low; at most 50% giveback), entry on the first 1-min close above the
third push's high; the `_vol` variant adds push volume above its pause's. Numbers in
`config/backtest.yaml` `rules: stairstep:` (`max_pause_bars`, `max_stair_bars` are
DEFAULTs you accepted for backtest only). Ten review rounds (G39–G48): NOT_CERTIFIED
rounds on rule fidelity and test gaps, all fixed; the last review of its commits found
nothing open. Look at: readings Q5.2-a/b and the run readout's `rule stairstep ...`
lines.

**#28 `entry-pullback-v1` (draft): Q5.3 "pullback".** After a volume spike, a touch of the
spike candle's high, no close below it, entry on the first up-close above the prior bar's
high, within `rules: pullback: lifetime_minutes` (DEFAULT 15, accepted for backtest only).
The PR title still says "slice 1"; all slices are built. Grind reviews G48–G51, last
**CONDITIONAL** with nothing open. Look at: readings Q5.3-a and Q5.3-d (for you).

**#29 `exit-trail-v1` (draft): Q5.4 "trail", the BASELINE.** The existing exit under a
name; the backtest builds trade management exactly as the live runner. Grind reviews:
NOT_CERTIFIED (G52-1: the docs called the trail volume-aware with scale-outs; the code
has neither), fixed; then **CONDITIONAL** (G53). Look at: reading **Q5.4-b**: your
approved text says "volume-aware trail (and scale-outs)", but the existing trail is a
fixed 50% lock of the fee-net peak and scale-out is not built. Nothing was added.

**#30 `exit-target-v1` (draft): Q5.5 "target".** Close the whole position when open
profit (on the bid, net of fees) reaches the existing `risk.profit_target`; no trail.
Latched (your 2026-10-11 10:30 PT approval, H1-0): once the target is reached, the
position closes at the bid as soon as an exit can begin, even if the bid has fallen back.
Grind reviews: **CONDITIONAL** (G53, G60, G61). Look at: row Q5.5-c (the latch).

**#31 `exit-time-v1` (draft): Q5.6 "time".** Ten minutes after the entry fill, close the
whole position if open profit is below +$250 (both APPROVED values in
`config/backtest.yaml` `rules: time:`); otherwise the baseline trail carries on. Judged
once; exactly +$250.00 keeps the trade. Slice 1 grind review: **CONDITIONAL** (G54);
slice 2 (`--exit time`, readout numbers): see rows Q5.6-b onward for its reviews. Readings
Q5.6-a (1)-(7) approved by you 2026-10-11 10:30 PT. Look at: the readout's `rule time`
lines.

## Q6 comparison runner (draft, #32)

**#32 `compare-v1` (draft) → exit-time-v1: Q6 comparison runner.** Backtest-only
tooling; it adds no rule, no number and no metric. (i) Review G52-2: every backtest
readout (readout.md, readout.json and the printed readout, the run with no rule named
included) now records the exit numbers it ran with, `risk.profit_target`,
`risk.trail_keep_pct` and `risk.stop_loss`, from the config the run loaded; the
no-option golden output changes only by those three lines per file. (ii)
`uv run python -m footprint.backtest.compare --bars F --out DIR` runs every entry rule
against every exit rule (sorted) by calling the single-run command with `--entry E
--exit X`, each into `DIR/<entry>__<exit>/`, then writes `summary.md` and `summary.csv`
with each run's verdict and go-live gate values copied from its readout.json, never
ranked or highlighted, headed "Synthetic or unverified data says nothing about
profitability." Grind review: **CONDITIONAL** (G67; one doc LOW, this paragraph). Look
at: readings **Q6-a** (the extra `--trade-from` option, not in your list) to **Q6-e** in
`docs/decisions-pending.md`. It has never been run on real data.
