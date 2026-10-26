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

## Full real file (review condition 3, branch backtest-speed-followup-v1)

Measured on the src of 5d2b0ca (PR #49's head 99f7d54 plus `BarWindow.__reduce__`,
which the backtest never calls; src identical to 90b9942 on this branch except that
main since added PR #50 and #51, which were not in the measured tree). Command:

    python -m footprint.backtest --bars nq_raw.csv --rolls data/rolls/nq_raw_rolls.csv \
        --trade-from 2017-01-01T00:00:00+00:00 --out DIR

The whole file (2010-06-06 .. 2024-09-27, 307 MB, about 4.8 million bars) is loaded;
trading starts 2017-01-01. `/usr/bin/time` is not installed here, so the process was
sampled from /proc every 15 s (VmRSS, VmHWM, utime, stime) and the journal's last
`as_of` read at each sample. Shared 2-core, 7.8 GB box; load average 3-7 throughout
(other sessions' pytest and mutation runs), so wall time is inflated; CPU time is the
cleaner number.

| measure | value |
|---|---|
| Load to the first decision (2017-01-03) | ~80 s wall, ~60 s user CPU |
| RSS after loading, before the first decision | 3.02-3.08 GB |
| Peak RSS (VmHWM) through 2017 | **3.45 GB** (3.41 GB at 2017-02-06, 3.45 GB at 2018-01-02: about +40 MB over the year) |
| 2017 traded (2017-01-03 .. 2017-12-29) | 138,890 decisions, 251 play days, 0 trades (equity 25,000 at 2018-01-02; why: below) |
| Time to finish 2017 | **7,496 s wall** (contended), **2,273 s user + 55 s sys CPU** |
| Rate | about 60 decisions per CPU-second (18.5 per wall-second on this busy box) |

Why 0 trades in 2017 (independent review of PR #55, from this run's journal, measured
on the src of 5d2b0ca): of the 251 play days (2018-01-02, also journalled, excluded),
**94 were PC-7 missing-data stand-asides** (37%), all caused by 6 bad history days
(2016-12-27, 2017-01-30, 02-22, 03-15, 05-01, 12-26), each blacking out up to 20
sessions; 75 were RULES stand-asides (opening efficiency neither trend nor chop); 54
CHOP; **28 TREND with entries allowed**. On those 28 TREND days every one of the 10,500
decisions at or after 10:00 ET was blocked by the entry gates (first failing gate:
trend direction 6,405, momentum 3,912); only 62 failed a single gate (53 momentum,
9 confirmer). So the system behaved as built; the causes are the PC-7 data blackouts,
the play caller's rules and CHOP, then the momentum and confirmer gates on TREND days.

The run was stopped (SIGTERM) at 2018-01-03 after 7,511 s: the full 2017-2024 span
would not finish in ~90 minutes here. **Inference, not measured:** at about 2,300 CPU-s
per traded year, 2017-01 .. 2024-09 is about 17,800 CPU-s, near 5 hours on an idle
core, and peak RSS should stay near 3.5-3.8 GB if the per-year growth holds. A
4-8 GB box is enough for one run; two at once on this box are not.

Not compared against the old code on the full file (it would take days at about 4
decisions a second). The naive `--trade-from 2017-01-01` (no UTC offset) stops with a
TypeError traceback before any decision; that parsing is pre-existing and untouched.
