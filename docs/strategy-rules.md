# Backtest rule library (Q5): the approved definitions

Andre's 2026-10-09 test queue, with the definitions he approved on 2026-10-10 10:55 PT.
His instruction: the rules must be "explicit, direct, precise and exact", with no room
for creative interpretation. The definitions below are copied word for word from the
grind task that carries his approval. Nothing here is a strategy decision for the live
system: every rule is a **backtest-only candidate**.

Marks used below:

- **APPROVED BY ANDRE 2026-10-10**: his wording and his numbers.
- **DEFAULT: not approved**: a number the definition needs but he did not give. It
  lives in config with a `# DEFAULT: not approved by Andre` comment and is listed in
  `docs/decisions-pending.md` under "Q5 numbers awaiting Andre".

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
quote, after every existing reason to get out (gap and hard stop first, then the
close, news, daily and weekly loss, kill switch). A yes closes the whole position with
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

DEFAULT: not approved (keys: added with its piece): maximum bars in one pause; maximum
bars for the whole stair (so slow sideways drift cannot qualify).

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

DEFAULT: not approved (keys: added with its piece): how many minutes after the spike
the setup stays alive.

## Q5.4 exit "trail" (`exit-trail-v1`): BASELINE

APPROVED BY ANDRE 2026-10-10:

> = the EXISTING exit, unchanged: hard loss stop, +$1,000 then the volume-aware trail
> (and scale-outs where contracts > 1). BASELINE. Expose it under the name; change no
> logic.

Config keys: the existing `risk.stop_loss`, `risk.profit_target` and trail keys. No new
keys.

## Q5.5 exit "target" (`exit-target-v1`)

APPROVED BY ANDRE 2026-10-10:

> when the position's open profit reaches +$1,000 (the existing configured target),
> close ALL contracts; no trail, no scale-out, no runner. Hard loss stop unchanged.

Config keys: the existing `risk.profit_target`. No new keys.

## Q5.6 exit "time" (`exit-time-v1`)

APPROVED BY ANDRE 2026-10-10:

> if 10 minutes after the entry fill the position's open profit is less than +$250,
> close the whole position. Otherwise the trade continues under the baseline "trail"
> exit. 10 minutes and $250 live in config (approved values). Hard loss stop
> unchanged.

The 10 minutes and the $250 are APPROVED values (keys: added with its piece).
