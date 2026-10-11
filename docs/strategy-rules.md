# Backtest rule library (Q5): the approved definitions

Andre's 2026-10-09 test queue, with the definitions he approved on 2026-10-10 10:55 PT.
His instruction: the rules must be "explicit, direct, precise and exact", with no room
for creative interpretation. The definitions below are copied word for word from the
grind task that carries his approval. Nothing here is a strategy decision for the live
system: every rule is a **backtest-only candidate**.

Marks used below:

- **APPROVED BY ANDRE 2026-10-10**: his wording and his numbers.
- **DEFAULT (accepted for backtest)**: a number the definition needs but he did not
  give. The builder picks the plainest value; Andre accepted those values FOR BACKTEST
  ONLY on 2026-10-10 21:12 PT (approval 3 in `docs/decisions-pending.md`). Each lives in
  config with a `# DEFAULT: accepted by Andre 2026-10-10 for backtest; revisit after
  real-data runs` comment and is listed in `docs/decisions-pending.md` under "Q5
  numbers awaiting Andre".

Config keys are filled in by the piece that builds each rule; a rule with no keys yet
is marked "(keys: added with its piece)".

## Scope (applies to every rule)

APPROVED BY ANDRE 2026-10-10:

> Scope: backtest-only. Candidate rules must NOT be wired into the live path and must
> NOT change the live/default behaviour. Every candidate entry goes through the SAME
> existing pipeline and gates as today's entry (15-min trend agreement, session
> windows, risk gate, kill switch, levels veto, sizing, hard stop); only the trigger
> differs. Every candidate exit sits on top of the SAME hard loss stop (never replaces
> or loosens it). Signals are only the types the current entry already emits (the
> system only buys options; never short; never sells what it does not hold). Up-move
> rules are written below; the down-move version is the exact mirror (highs<->lows,
> above<->below, up-close<->down-close).

> Synthetic bars prove a rule does what its definition says, NOTHING about
> profitability: never report or imply win rate, payoff, profit factor or any edge
> from synthetic data. Real-data comparison runs happen after 2026-10-14 and are
> Andre's call.

## Q5.0 rules seam (`rules-seam-v1`)

> let the backtest select a named entry rule and a named exit rule, e.g.
> `python -m footprint.backtest --bars F --entry breakout --exit trail`. With no option
> given, behaviour and output are byte-identical to today (prove with a test). Reuse
> the existing agents (src/footprint/agents/volume_reader.py, momentum_reader.py),
> decision pipeline and backtest engine; do not duplicate them.

How it is built (first slice, entry side): `--entry NAME` picks a factory from
`footprint.backtest.rules.ENTRY_RULES`; its trigger replaces only the momentum read in
`Pipeline` (`Pipeline(profiles, trigger)`); with no `--entry` the backtest builds
`Pipeline(profiles)` exactly as the live runner does. Proven byte-identical by
`tests/backtest/test_rules_seam.py` against golden files written by the pre-seam code.

Exit side (second slice): `--exit NAME` picks a factory from
`footprint.backtest.rules.EXIT_RULES`. Trade management asks the rule last, on a fresh
quote, after every existing reason to get out. In code order: a resting stop that is
filled, held back, being cancelled by someone else or gone, or no resting stop at all
(each exits before the rule is reached); a stale quote or a vanished bid (the rule is
not asked); then the gap, the hard stop, the close, news, the news time stop, the daily
loss limit, the weekly loss limit and the kill switch; the rule comes last. A yes closes the whole position with
exit reason `RULE`. The rule can only add a way out; it never moves, replaces or
loosens the hard stop. With no `--exit` trade management is built exactly as the live
runner builds it.

## Q5.1 entry "breakout" (`entry-breakout-v1`): BASELINE

APPROVED BY ANDRE 2026-10-10:

> = the EXISTING momentum entry, unchanged: a 1-min volume-spike candle in the trade
> direction (the existing relative volume threshold) followed by a same-direction
> follow-through candle, entry as the code does it today. This is the BASELINE. Expose
> it under the name; change no logic. If anything about the existing entry is
> ambiguous, record it for Andre; do not change it.

Config keys: the existing `strategy.volume.*` keys (`spike_multiple`, lookback, timeframe).
No new keys.

How it is built (`entry-breakout-v1`): `footprint.backtest.rules.ENTRY_RULES["breakout"]`
builds the existing momentum reader (`agents/momentum_reader.py`) from the same
`strategy.volume` settings the pipeline uses when no rule is named. No logic changed.
`tests/backtest/test_entry_breakout.py` proves a `--entry breakout` run writes the same
files, decisions and trades as the default run; the only difference is the readout line
`entry rule: breakout`.

What the existing entry does today, exactly (from the code, for Andre to confirm; not
changed): the spike is the second-to-last completed 1-min bar; its volume divided by
the average volume of the same time of day over the last `lookback_sessions` sessions
must be at least `spike_multiple` (fewer than `lookback_sessions` earlier same-time bars, or
an average of zero: no entry); its direction is close versus open (close equal to
open is a doji: no entry). The follow-through is the very next bar, and it only has to
CLOSE beyond the spike bar's CLOSE in the same direction; it does not have to be an
up-close candle itself or clear the spike's high. Both conditions are judged on the
bar that just completed; the trend, session and other gates then decide as always.

## Q5.2 entry "stairstep" and "stairstep_vol" (`entry-stairstep-v1`)

APPROVED BY ANDRE 2026-10-10:

> on 1-min bars. A PUSH is a run of consecutive bars each making a new high above the
> run's prior high; a PAUSE is the bars after a push until a bar makes a new high above
> that push's high; the pause low is the lowest low in the pause. A STEP is a push
> followed by its pause. A step is valid only if (a) its pause low is above the
> previous step's pause low (higher low), and (b) the pause gives back at most 50% of
> its push (push high minus pause low <= 0.5 x (push high minus the low the push
> started from)). Three consecutive valid steps form the stair. ENTRY: the first 1-min
> bar that CLOSES above the high of the third push. Any invalid step resets the count
> to zero. Run as two named variants: "stairstep" (no volume condition) and
> "stairstep_vol" (additionally, each push's average bar volume is greater than the
> average bar volume of the pause that follows it).

The 50% giveback and the count of three steps are APPROVED values.

Config (`config/backtest.yaml`, `rules: stairstep:`, read by the backtest only):
`steps: 3` and `max_giveback: 0.5` (APPROVED BY ANDRE 2026-10-10);
`max_pause_bars: 10` (maximum bars in one pause) and `max_stair_bars: 60` (maximum bars
for the whole stair, so slow sideways drift cannot qualify): DEFAULT, accepted for
backtest.

How it is built (`entry-stairstep-v1`, slice 1 of 2: the detector): 
`footprint.backtest.stairstep.stair_entries` reads the definition above on a run of
1-min bars and returns the bars where the entry fires. How each word is read is in
`docs/decisions-pending.md` (Q5.2-a); every reading is pinned by a hand-built fixture in
`tests/backtest/test_stairstep.py`. Slice 2a (`StairTrigger`) puts the detector in the
momentum slot; slice 2b-1 loads the four numbers from config (above). Slice 2b-2
registers "stairstep" and "stairstep_vol" in `ENTRY_RULES` (`--entry stairstep`,
`--entry stairstep_vol`); each factory builds the trigger from `config/backtest.yaml`
`rules: stairstep:` on the pipeline's completed 1-min bars. Every other gate still
decides, exactly as for "breakout" (reading Q5.2-l, for Andre to confirm).
A stair run's readout lists the four stairstep numbers it ran with (`rule stairstep ...`
under assumptions; review G47-1), so runs on different DEFAULTs can be told apart.
Until the entry close the steps still count: if the step after the formed stair turns
invalid first, the count is zero and there is no entry (review G39-1; reading Q5.2-b,
for Andre to confirm).
A pause past `max_pause_bars` or a stair past `max_stair_bars` is an invalid step like
any other: the pause runs on until a bar exceeds its push high, and that push starts the
new count, so no stair forms inside the old pause (review G40-1; reading Q5.2-c, for
Andre to confirm).

## Q5.3 entry "pullback" (`entry-pullback-v1`)

APPROVED BY ANDRE 2026-10-10:

> after a valid volume-spike candle in the trade direction (same spike test as
> "breakout"), price dips back to the HIGH of that spike candle, holds it, and turns
> back up. Exactly: (a) a later 1-min bar's low trades at or below the spike candle's
> high (touch); (b) no 1-min bar CLOSES below the spike candle's high (hold; a close
> below cancels this setup); (c) ENTRY on the first 1-min bar after the touch that
> closes up (close > open) and above the prior bar's high.

> If a "breakout" entry already fired on the same spike, the pullback rule is a
> separate test run and does not stack positions (one position at a time).

DEFAULT, accepted for backtest: how many minutes after the spike the setup stays alive
(`config/backtest.yaml` `rules: pullback: lifetime_minutes`, 15; slice 2a).

How it is built (`entry-pullback-v1`, slice 1: the detector): `footprint.backtest.pullback.pullback_entries`
takes 1-min bars and the spike test's answer per bar and returns the bars where the
entry fires. How each word is read is in `docs/decisions-pending.md` (Q5.3-a, for
Andre to confirm); every reading is pinned by a hand-built fixture in
`tests/backtest/test_pullback.py`. Slice 2a: the lifetime DEFAULT loads from
`config/backtest.yaml` `rules: pullback:`. Slice 2b-1: `spike_score` is the spike test
"breakout" uses, read as the spike bar test alone (MomentumReader step 1, pinned against
MomentumReader and VolumeReader; reading Q5.3-d), and `PullbackTrigger` puts the
detector in the momentum slot of the decision pipeline. Slice 2b-2: "pullback" is in
`ENTRY_RULES` (`--entry pullback`); the factory builds the trigger from the live
`strategy.volume` numbers and session time zone (the ones "breakout" uses), the
`rules: pullback:` lifetime and the 1-minute entry bars, and the readout records
`rule pullback lifetime_minutes`. Every other gate still decides; the live path and the
run with no rule named are unchanged.

## Q5.4 exit "trail" (`exit-trail-v1`): BASELINE

APPROVED BY ANDRE 2026-10-10:

> = the EXISTING exit, unchanged: hard loss stop, +$1,000 then the volume-aware trail
> (and scale-outs where contracts > 1). BASELINE. Expose it under the name; change no
> logic.

Config keys: the existing `risk.stop_loss`, `risk.profit_target` and trail keys. No new
keys.

Built (`exit-trail-v1`): `--exit trail`. The existing exit is trade management itself
(hard stop and gap first, then every existing reason, then the trail), so "trail" adds
no exit rule: its factory gives None and the backtest builds trade management exactly
as the live runner and the run with no rule named do. A `--exit trail` run writes the
same files as the default run; the only difference is the readout's assumption line
`exit rule: trail`.

What the existing exit actually is (review G52-1; reading Q5.4-b, APPROVED BY ANDRE 2026-10-11 10:30 PT): once the
gain on the bid, net of round-trip fees, reaches `risk.profit_target` ($1,000), the stop
moves to lock `risk.trail_keep_pct` (50%) of the peak net gain and then follows the
peak at that share (`src/footprint/decision/stop.py`, decision D13). **The trail has no
volume adjustment** ("No volume or theta adjustment until those agents exist"), and
**scale-out is not built**: `risk.max_contracts` must be 1, so "(and scale-outs where
contracts > 1)" never applies. The approved wording above says "volume-aware trail";
the baseline is the trail that exists, unchanged, and nothing was built to make it
volume-aware.
Andre confirmed this baseline on 2026-10-11 10:30 PT: build no volume-aware trail;
any future one is a new piece that he will define.

## Q5.5 exit "target" (`exit-target-v1`)

APPROVED BY ANDRE 2026-10-10:

> when the position's open profit reaches +$1,000 (the existing configured target),
> close ALL contracts; no trail, no scale-out, no runner. Hard loss stop unchanged.

Config keys: the existing `risk.profit_target`. No new keys.

Built (`exit-target-v1`): `--exit target`. `footprint.backtest.exits.TargetExit`, built
from `risk.profit_target`, is handed to trade management as its exit rule. Trade
management asks it last on every fresh quote, after the hard stop, the gap and every
other existing reason; when it says out, the whole position leaves through the existing
exit (limit at the bid, the resting stop cancelled first, never sold twice). Readings
Q5.5-a (APPROVED BY ANDRE 2026-10-11 10:30 PT): the open profit is trade management's figure, on the bid and net of
round-trip fees (the same basis as the trail and every risk check); "reaches" is at or
above, so exactly +$1,000.00 closes and one option tick below does not; ALL contracts is
the one position (scale-out is not built, `risk.max_contracts` is 1). "No trail" needs
nothing extra: trade management asks the rule before it moves the trail, and the trail
cannot engage before a quote at the target, so the trade is out first.

Q5.5-b LATCHED (APPROVED BY ANDRE 2026-10-11 10:30 PT; built in HARDENING H1-0): once
the open profit reaches the target, the target verdict is held for that position until
it is gone, the same way the "time" rule holds an "out". If the exit cannot begin yet
(the broker's positions uncertain, or a refused cancel of the resting stop, which then
still protects) it waits exactly as before; it then closes as soon as an exit can
begin, at the bid then, even if the bid has fallen back below the target. Limit at the
bid, the stop cancelled first, never sold twice; the hard stop unchanged and asked
first. Verdicts are per position, and a new position starts unlatched. Backtest-only
(`TargetExit`); trade management and the live path are unchanged.

## Q5.6 exit "time" (`exit-time-v1`)

APPROVED BY ANDRE 2026-10-10:

> if 10 minutes after the entry fill the position's open profit is less than +$250,
> close the whole position. Otherwise the trade continues under the baseline "trail"
> exit. 10 minutes and $250 live in config (approved values). Hard loss stop
> unchanged.

The 10 minutes and the $250 are APPROVED values (keys: added with its piece).
