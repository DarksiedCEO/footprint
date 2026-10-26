# Running Footprint on the IBKR paper account

Everything here runs on your Mac, beside IB Gateway. Paper only: the program refuses
to start against a live account or with `ibkr.require_paper: false`.

## Before the first run

1. IB Gateway is running, logged in to the **paper** account, API settings as in the
   paper checklist (port 4002, localhost only, Read-Only off, "Maintain and resubmit
   orders when connection is restored" **unchecked**).
2. `.env` in the repo folder has `BROKER_HOST`, `BROKER_PORT`, `BROKER_ACCOUNT_ID`,
   `BROKER_CLIENT_ID` (the same values the read-only check used). Never commit it.
3. The read-only check passes: `uv run python -m footprint.brokers.ibkr.check`.
4. Real-time CME data on the paper login. Without it every quote is refused: the chain
   stays empty, so no new trade is ever opened. But nothing can be priced either: if a
   position is left over from an earlier run, the system will not sell it blind and
   asks you (an alarm) to flatten it by hand in IBKR.
5. A news calendar CSV (`name,start,end`, ISO times **with offsets**, e.g.
   `CPI,2026-10-14T08:28:00-04:00,2026-10-14T08:32:00-04:00`), or decide to run
   without one (`--no-blackouts`).

## Start

First run only (creates `state/ledger.json`):

    uv run python -m footprint.live --blackouts news.csv --new-ledger

Every later run:

    uv run python -m footprint.live --blackouts news.csv

It starts reconciling and **sends nothing** until you clear it. At the prompt:

| Type | Does |
|---|---|
| `status` | Connection, front future, bars loaded, chain size, reconcile/halt state, position |
| `clear Andre` | Allows entries (logged as an alarm with your name) |
| `quit` | Stops cleanly (also Ctrl-C) |

Loading 35 days of history takes about a minute after the connection opens
(`status` shows the bar count growing from 0).

## Stopping with a position open

`quit` (or Ctrl-C) stops managing at once; it does not flatten. It warns you if
anything is held or working. After that only the resting stop at IBKR protects the
position, and it is a DAY order: it lasts until IBKR's trading session ends. The next
start flattens anything still held (priced at the market once a quote arrives; never
sold blind). Flatten by hand in IBKR, or check the stop there, before you walk away.

## Gateway restarts and disconnects

IB Gateway restarts itself every night. A disconnect is not a fault: the system waits,
reconciles once IBKR is back, and carries on (D35). After 5 failed reconnects in a row
it alarms once that a person is needed and retries less often (D32). A halt (a real
disagreement: something held that should not be, an order that will not cancel) only
clears with a restart, after you have looked at why.

## Where things go (all in `state/`, git-ignored)

- `ledger.json`: equity, drawdown peak, breakers, kill switch. Never delete it to
  "reset"; a missing ledger is refused unless `--new-ledger`.
- `journal/`: every decision and trade.
- `alarms.log`: every alarm, one JSON line each (also printed in the terminal). Until
  an alarm channel is chosen (D20), this terminal and file are the only places alarms
  reach you.

## Holidays and early closes

`instruments/nq.yaml` lists the exchange's closed days and early closes through
`calendar_through`. On an early-close day the system stops new entries and flattens
5 minutes before that day's close (e.g. 09:55 PT for a 10:00 close). A month before
the list runs out it alarms once a day until it is extended (checklist 23).

## Alarms you should expect at first

- `exchange calendar ends ...`: a month before `calendar_through`; extend nq.yaml.
- `MARKET DATA: ... failed (IBKR 354 ...)`: no real-time subscription.
- `no option on the ... future expires in 1-3 days`: in a roll week (D27).
- `IBKR SESSION KEEPS FAILING`: Gateway closed, logged out, or the Mac asleep (D32).
