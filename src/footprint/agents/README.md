# Agents (floor 2)

The crew. Agents that form an opinion about the market implement
`footprint.core.interfaces.Reader` and return a `Read` (a score of -1, 0 or +1
plus a reason). A `Read` never carries an order.

The roster below is the whole v1 system, so it also names the roles that live
on the decision and execution floors.

## v1 roster

System ids are permanent. Names are display labels only and may change.

| System id    | Name      | Job |
| ------------ | --------- | --- |
| `trend`      | Mateo     | Reads the trend timeframe (higher highs/lows vs the fast/slow EMA pair) |
| `momentum`   | Darius    | Hunts the entry-timeframe candle (volume spike + follow-through) |
| `volume`     | Rafael    | Relative volume vs the trailing average for the time of day |
| `levels`     | Grant     | Distance to prior-day H/L/C, overnight H/L, VWAP, swings, round numbers |
| `range`      | Nadia     | Today's range vs the average daily range |
| `session`    | Cyrus     | Session windows and news blackouts |
| `sizing`     | Ezra      | Position size within the risk caps |
| `stop`       | Booker    | Hard stop placement and the trail |
| `execution`  | Diego     | Limit-only order placement |
| `zeroday`    | Nia       | Flags markets that match no known pattern; tells risk to stand down |
| `theta`      | (unnamed) | Decay veto: modelled Black-76 decay over the planned hold vs its budget |
| `console`    | Desmond   | Scores reads, rules only, emits Signals |
| `risk`       | Dolores   | Final veto, daily/weekly limits, kill switch |
| `trade_mgmt` | Malik     | Manages the open trade |
| `journal`    | Josephine | Records every trade's full fingerprint |

## Inputs and conventions

- A `Bar`'s `ts` is the moment the bar opens. Agents only ever receive completed
  bars: `footprint.data.aggregate` drops any bar still forming (G12).
- Daily bars, prior-day levels and VWAP use the instrument's cash session (G10, G11).
- Readers that score a trade (`levels`, `range` via `assess`) return +1 when they
  support it, 0 when neutral and -1 when against; a -1 from `levels` is a veto.
- `zeroday` returns a `ZeroDayCheck`; tripped means no new entries.
- `theta` returns a `ThetaCheck` on a sized entry; not allowed means no entry.
