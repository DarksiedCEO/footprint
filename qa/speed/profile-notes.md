# BH-11 backtest speed-up: profile notes (2026-10-26)

Dataset: `tests/backtest/speed_market.py`, 130 synthetic sessions from 2024-03-11
(15:00 PT the evening before to 13:59 PT, every minute: 179,400 bars), seed 11;
command line as shipped (reference_price 20000, `--rolls none`), traded from
2024-09-11T00:00Z: the last three sessions, 1,801 decisions, the real play caller
(3 days labelled). Same CSV, same 2-core machine, both runs alone.

| | before (main 928ca71) | after (backtest-speed-v1) |
|---|---|---|
| wall time, whole command line | 464.2 s | 38.9 s |
| decisions per second | 3.9 | 46.3 |
| peak RSS (measured with another job running) | 168 MB | 160 MB |
| outputs | reference | byte-identical (journal, ledger, readouts, stdout) |

Wall time includes reading the CSV, the setup and the readout (a few seconds).
Synthetic timing on a 179,400-bar file is NOT a promise about the real 2017-2024
(or 2010-2024) run: real data has gaps, a longer history (the play caller's history
grows with it) and other trade counts. Measure on the machine that runs it.

## Hotspots before (cProfile, 1,023 s profiled, same dataset)

Cumulative seconds, 1,801 decisions; each re-scanned the whole 43,200-bar window:

| function | cum s | what it did per decision |
|---|---|---|
| zero_day.check | 269 | Decimal lists of every bar's volume, true range and jump; today's and 20 days' ranges by a full pass |
| aggregate.resample | 247 | the window re-bucketed into 15-minute bars (twice: trend, levels; cached once) |
| _series.relative_volume | 148 | a Decimal list of every volume, twice (momentum, volume) |
| pipeline._range_days / session_days | 147 / 142 | the window regrouped into cash-session days |
| _series._clock_index | 118 | a clock-time index rebuilt for every new window tuple |
| aggregate.completed | 102 | all(...) over every bar, four or five times |
| series.require_series | 84 | the window re-checked bar by bar |
| levels.snapshot | 55 | three linear scans, a set of every start time |

## After (cProfile, 60 s profiled, same dataset, final code)

The window helpers now reuse per-series work (see the BH-11 row in
docs/decisions-pending.md). Left, cumulative seconds: the decision pipeline 31.5, of
which the trend reader 14.2 (its two EMAs over the window's ~2,880 15-minute bars,
exact Decimal arithmetic, kept: 7.2; its own check of that list: 5.5), levels 7.4,
zero-day 5.4; the option model's chain 8.6; the runner's own pass over the window
6.5 (`execution/runner.py`, not touched: out of scope); the ledger's fsync 3.2 and
the journal 2.9 (execution, not touched).
