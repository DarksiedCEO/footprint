# London session test (london-v1): pre-registration

Status: PRE-REGISTERED. Written and committed before any code for the rule exists and
before it has been run on any data. Nothing below may change after a result is seen;
a change makes it a new hypothesis that counts against the luck bar.

Data: NQ continuous 1-minute futures, 2010-2024, with Cboe's VXN daily history as the
option's implied volatility (UNVERIFIED). This data has been used for earlier RTH (New
York cash session) research, but never for London-session rules.

All times are New York (America/New_York) wall-clock time.

## L1: London opening-range breakout

- **Range:** the high and low of the 1-minute bars opening 03:00 to 03:14 (03:00-03:15 ET).
- **Entry:** between 03:15 and 07:00 ET, the first 1-minute close above the range high
  buys a call; the first close below the range low buys a put. The entry is at that
  bar's close. Precisely: trigger bars open at 03:15 through 06:59 (their closes fall
  03:16 through 07:00). "First" applies per trade: after a trade closes (and before
  the per-session limit), the next close outside the range in that window may enter.
- **No range, no trade:** a session missing any of the 15 range bars (03:00-03:14) is
  not traded.
- **Setup stop:** the opposite side of the range (a futures price), or the option-dollar
  booker exit, whichever comes first.
- **Green stop:** no new entry once the session's realized P&L (net dollars of its
  closed trades, costs included) is at or above `green_stop_dollars`. Grid {500, 1000} (Andre's range). Both are reported; nothing
  else is tried. An open trade keeps riding its exit after the green stop is hit.
- **Hard flat:** 08:25 ET. One position at a time. At most `max_trades_per_session`
  trades per session, pre-registered 2.
- **Exits:** `booker` (1 contract) and `scale_out` (2 contracts), as built in
  research-v1 (stop risk.stop_loss; lock risk.trail_keep_pct of the peak net open
  profit from risk.profit_target; scale-out first target risk.profit_target).
- **Option:** moneyness 0 (at the money); days to expiry 1 and 7, both reported.
  Valued with Black-76 every minute, rescaled to the reference price level. Black-76
  prices a European option: the model stands for the weekly (European-style) NQ
  options, not the American-style quarterlies.
- **Costs:** round trip per contract from config, pre-registered 170 (the measured NQ
  overnight cost; UNVERIFIED for London hours).

## Scenarios (8, judged once each)

green stop {500, 1000} x exit {booker, scale_out} x days to expiry {1, 7}.

## Judging

Judged ONCE per scenario. Pass = the go-live gates in config/backtest.yaml (trades,
out-of-sample trades, win rate, payoff, profit factor, max drawdown, on the whole run
and on the held-out last 30%) AND at least 2 of 3 time thirds (by trade order) with
positive net. Eight scenarios are run, so one pass among them is weaker evidence than a
single pre-registered test: a pass is a candidate for paper trading, not a proof.

## Honest limits

- Option prices are modelled (Black-76 on the VXN close as IV), not quoted. UNVERIFIED.
- The 170 cost was measured overnight, not in London hours. UNVERIFIED.
- The range rule reads only bars up to the one just closed (tested: no lookahead).
