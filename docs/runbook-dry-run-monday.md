# Supervised dry run on paper, before market data (Monday 2026-10-12)

The goal: start the system, read its status, check the kill switch, survive a
deliberate Gateway disconnect, shut down cleanly and restart, all on the paper login
with **no market data subscription**. No market data means no quotes, so no option
can be priced and **no entry can be opened**. This run tests the plumbing, not trading.

You stay at the terminal the whole time. Everything runs on your Mac in the repo
folder. What each step prints is quoted from the code. **UNVERIFIED until you run it:**
nothing here has run against the real Gateway yet.

## 0. Before you start

1. IB Gateway is running and logged in to the **paper** login (DUR239136), with the API
   settings from the paper checklist: port 4002, localhost only, Read-Only API off,
   master API client id 0, "Maintain and resubmit orders when connection is restored"
   **unchecked**.
2. `.env` holds `BROKER_HOST`, `BROKER_PORT`, `BROKER_ACCOUNT_ID`,
   `BROKER_CLIENT_ID=0`.
3. After any `uv sync`: `uv run python scripts/install_ibapi.py`.
4. Nothing else is connected with client id 0 (no `check`, `history` or
   `record_chain` running).
5. The paper account holds nothing and has no working orders in NQ options (look in
   IBKR's own platform).

## 1. Read-only check

    uv run python -m footprint.brokers.ibkr.check

Expect: `connecting to IB Gateway at <BROKER_HOST>:4002 for DUR239136`, then
`resynced. account DUR239136; system scope NQ FOP`, `positions in scope: none`,
`open orders in scope: 0`, and exit code 0. If it says `REFUSED` or `NOT READY`, stop
here.

## 2. Kill switch status before the first start

    uv run python -m footprint.live.kill_switch status

- Before the very first start: `Refused: there is no ledger at state/ledger.json;
  nothing was created.` That is expected.
- If a ledger exists: `kill switch: not latched`, `equity: …   drawdown peak: …`,
  `drawdown limit: not breached`, `open position: none`. If it says LATCHED, stop and
  find out why before going on (see "Gateway restarts and disconnects" in
  `live-paper-runbook.md`).

## 3. Start

First run only (creates `state/ledger.json`):

    uv run python -m footprint.live --no-blackouts --new-ledger

Every later run: the same command without `--new-ledger`. A news calendar is not
needed for the dry run. If you have not set up all six phone-alarm keys yet (D20), add
`--alarms-local-only`: alarms then go to the terminal and `state/alarms.log` only, and
it prints a DEGRADED_ALERTING banner.

Expect: `PAPER account DUR239136 at <BROKER_HOST>:4002; reconciling first; no entries until
'clear <name>'`.

**Do not type `clear`** in this dry run. Without `clear`, no entry is ever sent.

## 4. Status

Type `status`. Expect these lines (values will differ):

    alerting: FULL: Telegram, SMS fallback, calls for critical alarms   (or the degraded mode)
    cleared to trade: no
    time 2026-10-12T…
    IBKR ready: True; refused: no
    front future: …                     (or "not yet")
    bars: 0; newest none                (no data subscription: the history is refused)
    chain: none quotes
    reconciling: False; halt: no
    position: none
    alarms waiting: 0

Without the subscription, expect alarms like `MARKET DATA: … failed (IBKR 354 …)`.
That is the expected result today, not a fault.

## 5. Kill switch while running

In a **second** terminal:

    uv run python -m footprint.live.kill_switch status

Expect: `Refused: the ledger is in use. stop the live system first, then run this
again.` This is correct: the running system holds the ledger.

## 6. Deliberate Gateway disconnect and reconnect

1. In IB Gateway: **File → Exit** (or close its window and confirm). The system keeps
   running.
2. Expect the alarm `IBKR connection lost: nothing is sent until it is back and
   resynced`. With full alerting, your phone rings.
   `status` now shows `IBKR ready: False`. Nothing is sent while disconnected.
3. Wait about a minute. If the Gateway stays down through 5 failed reconnects, expect
   `IBKR SESSION KEEPS FAILING` once.
4. Start IB Gateway again and log in to the **paper** login.
5. Expect it to reconnect and resync by itself. `status` shows `IBKR ready: True` and,
   once the reconcile agrees, `reconciling: False; halt: no`.
6. If `halt:` lists a reason, write it down and send it to me. Do not clear anything.

## 7. Clean shutdown

Type `quit`. Expect `stopping`. With nothing held there is no WARNING line. A
`WARNING: stopping with … held or working` line would mean something is open: look in
IBKR at once.

**Use `quit`, not Ctrl-C.** Ctrl-C also stops the system, but it skips the
held-position check: it prints neither the WARNING line nor `stopping`, so an open
position would go unmentioned.

Then:

    uv run python -m footprint.live.kill_switch status

Expect `kill switch: not latched`, `open position: none`, and no CHECK THE FILL hold.

## 8. Restart

    uv run python -m footprint.live --no-blackouts

(Add `--alarms-local-only` again if you used it.) Expect the same start line as in
step 3, and `status` as in step 4. Then `quit`.

## What to send me afterwards

- The terminal output of steps 1 to 8, and `state/alarms.log`.
- Anything that differed from what is written here. Each difference is either a doc
  fix or a finding.
