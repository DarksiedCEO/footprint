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
4. Real-time CME data on the paper login (otherwise every quote is refused, the chain
   stays empty and nothing trades: safe, but nothing to see).
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

## Where things go (all in `state/`, git-ignored)

- `ledger.json`: equity, drawdown peak, breakers, kill switch. Never delete it to
  "reset"; a missing ledger is refused unless `--new-ledger`.
- `journal/`: every decision and trade.
- `alarms.log`: every alarm, one JSON line each (also printed in the terminal). Until
  an alarm channel is chosen (D20), this terminal and file are the only places alarms
  reach you.

## Alarms you should expect at first

- `STARTED WITHOUT HOLIDAYS`: until the CME holidays are filled in (D34).
- `MARKET DATA: ... failed (IBKR 354 ...)`: no real-time subscription.
- `no option on the ... future expires in 1-3 days`: in a roll week (D27).
- `IBKR SESSION KEEPS FAILING`: Gateway closed, logged out, or the Mac asleep (D32).
