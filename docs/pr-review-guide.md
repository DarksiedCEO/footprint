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
PRs #22 → #23 → #24, then the Q5 backtest-rule PRs as they open.

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
contract, the $5,000 cap held at the worst allowed fill, the $750 hard stop.

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

**#11 `ibkr-v1`: IBKR order adapter, tested against a fake IBKR.** Nothing sent before
a full resync, no guessed positions, one order per id, stop and exits in one OCA group.

**#12 `ibkr-binding-v1`: official ibapi 10.50 binding and the read-only first look**
(`python -m footprint.brokers.ibkr.check`). Paper only, one account, one instrument,
exact prices. Look at: that your stock is only ever "open elsewhere".

**#13 `restart-memory-v1`: a restart never decides the same minute twice.**
**CERTIFIED** at 6ee5ec1.

**#14 `market-data-v1`: market data connection** (contracts, chains, quotes, bars).
Real-time only; a market data fault never touches orders.

**#15 `market-service-v1`: market data service** (front future, spliced back-adjusted
history, polled bars, option chain). Look at: the roll 8 days before expiry and the
100-point / 80-line chain limits (config defaults).

**#16 `session-backoff-v1`: reconnects escalate once, then back off.** **CERTIFIED**
at 6982b3d.

**#17 `live-startup-v1`: the live start-up program** (`python -m footprint.live`). Paper
only, no entry until `clear <name>`, `--new-ledger` only on the first run, `quit` warns
if anything is held.

**#18 `resync-realarm-v1`: a stuck resync is alarmed again, also while flat.**
**CERTIFIED** at 4edc489. Decision D36 (the 300 s repeat).

**#19 `holidays-v1`: exchange calendar, flatten 5 min before an early close (D34).**
Look at: the holiday dates are **UNVERIFIED** against CME's own 2026/27 pages, and the
calendar ends 2027-03-31 (alarm 30 days before).

**#20 `alarm-channels-v1`: phone alarms (D20).** Telegram, SMS fallback, calls for
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

## Q5 backtest rule library (to come)

Each Q5 piece (rules-seam, entry breakout, stairstep, pullback, exit trail, target,
time) will open as its own draft PR stacked on #24. They are backtest-only: the live
entry and exit do not change. This guide is updated as each one opens.
