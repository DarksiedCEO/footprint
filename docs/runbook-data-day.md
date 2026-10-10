# Data day (2026-10-14): first real market data

What to do once the deposit has cleared, in order. Everything runs on your Mac in the
repo folder, beside IB Gateway logged in to the **paper** login (account DUR239136).
Every command below is read-only: none can place, change or cancel an order (they all
go through the read-only gateway and refuse a non-paper account, port 4001 or port
7496). Run them **one at a time**: every tool connects with client id 0, so two at once
cannot share the Gateway.

Nothing here has been run against the real Gateway yet. What each command prints is
what the code and its tests say it prints. **UNVERIFIED until you run it.**

## 1. Subscribe to CME real-time data (live login, Client Portal)

The subscription is bought on your **live** login and then shared with the paper login.
The labels below are IBKR's as published in their help pages; if your screen differs,
stop and send me a screenshot. Send a screenshot before every **Save** or **Continue**
that costs money.

1. Log in to Client Portal with your **live** username.
2. Click the head-and-shoulders icon (top right) → **Settings**.
3. Under Account Settings, find **Market Data Subscriptions** → **Configure**.
4. Choose **North America** and tick **CME Real-Time (NP,L1)** only. It must say
   **NP** (non-professional). Do not tick bundles or anything else.
5. Accept the subscriber agreements, check the monthly fee shown, screenshot, then
   confirm.

## 2. Share it with the paper login

1. Still on the live login: head-and-shoulders icon → **Settings** → **Paper Trading
   Account**.
2. Turn on **Share real-time market data subscriptions with paper trading account**.
   Screenshot, then **Save**.
3. Sharing can take some time to reach the paper login (IBKR does not promise it is
   instant). If step 4 below shows code 354 or 10167, wait and try again later.
4. Do not stay logged in to the **live** login in TWS or the mobile app while the
   paper Gateway reads data. IBKR sends real-time data to only one session, so a live
   session at the same time can cost the paper login its data (IBKR error 10197).

## 3. Restart IB Gateway on the paper login

Log out of IB Gateway and back in to the paper login, so it picks up the shared
subscription. Same API settings as before (port 4002, localhost only, Read-Only off,
"Maintain and resubmit orders when connection is restored" unchecked).

## 4. Market-data probe (Q1)

    uv run python -m footprint.brokers.ibkr.check --market-data

It runs the usual resync report (positions in scope, open orders), then waits up to
`market_data.probe_wait_seconds` (config) for quotes.

- **Good:** `future <id>: bid … ask …, <age>`, then `option quotes (N of M fresh,
  nearest the future first):` and one line per option (expiry, strike, right, bid,
  ask, quote age). Exit code 0. A missing bid prints as `none`, never as a price.
- **No data:** `NO MARKET DATA after N s: <what is missing>`, with IBKR's reason in
  the `ALARM` lines above it, for example code `354` (not subscribed) or `10167`
  (delayed data only). Exit code 1. Go back to step 2 (sharing not active yet) or
  step 1.
- Stop here if the probe does not pass. The next steps need live quotes.

## 5. Export the history (Q2)

    uv run python -m footprint.brokers.ibkr.history --out data/nq_1m.csv

It loads `market_data.history_days` days of 1-minute bars of the traded future (spliced
and back-adjusted across rolls, Saturdays skipped, requests paced), waiting up to
`market_data.export_wait_seconds`. `--days N` changes the length for one run.

- **Good:** the file is written and the exit code is 0.
- It **refuses to overwrite** an existing file. Pick a new name, or move the old one.
- **A gap or an error:** the exit code is 1 and no `data/nq_1m.csv` appears. If it is
  refused for many short holes, look at `market_data.export_max_missing_minutes`
  (decision Q2-a), not the rule.

## 6. First real-data backtest

    uv run python -m footprint.backtest --bars data/nq_1m.csv --out runs/first-real

`--out` must not exist yet or must be empty. It writes `readout.md` and
`readout.json`. Exit codes: 0 PASS, 1 FAIL or NOT ENOUGH TRADES, 2 INVALID.

One export of about 35 days will not reach the go-live gates (200+ trades, 60+ out of
sample). Treat this first run as a check that the pipeline works on real prices, not
as evidence of an edge. The option prices in the backtest still come from the model
(D22), which the chain recording (step 7) is there to calibrate.

## 7. Record the option chain (Q3)

    uv run python -m footprint.brokers.ibkr.record_chain --out data/chain/ --minutes 390

It writes one snapshot every `market_data.record_interval_seconds` for `--minutes`
(default `market_data.record_minutes`) to one CSV per UTC day in `data/chain/`. Each row
holds the time, future price, expiry, strike, right, bid, ask, quote age, and the open
interest when IBKR sent it (blank when it did not).

- It writes only fresh quotes (no older than `quotes.max_age_seconds`). A disconnect or
  a minute with no fresh chain writes nothing and says so, and the recording goes on.
- An `ERROR` line means a snapshot was refused or could not be written. The run goes
  on but ends with exit code 1. A torn day file is named on every interval: leave it
  for me to look at.
- The run blocks the Gateway's client id 0. Do not start the paper runner until it
  ends (or Ctrl-C it).
- What to look at afterwards, for your decisions: open interest by strike (D8),
  spreads and quote ages (D22, Q3-c).

## After the data day

Nothing changes in config, live behaviour or the go/no-go gates without your
decision. The backtest rule comparisons (Q5) on this data are your call.
