"""Build the roll-session file of a raw front-month history from its Databento source
(backtest-history-v1, decision BH-9; review round 2, R2-1).

    uv run python scripts/build_rolls.py NQ_DATABENTO_OHLCV_1M.csv data/rolls/nq_raw_rolls.csv

The source is Databento OHLCV-1m of the parent symbol (columns ts_event, instrument_id,
volume, symbol among others), decompressed. The rule is the history builder's own (the
script that made nq_raw.csv), so the sessions found are exactly where the raw series
switches contract:
- outrights only: a symbol with "-" is a spread and is skipped;
- the session (CME trade date) of a bar is the New York date of (ts_event + 6 hours):
  18:00 New York opens the next session;
- the front contract of a session is the outright with the most volume in it (a tie
  goes to the higher instrument id, as the builder's max() does);
- the front only ever moves FORWARD, to a contract whose last bar is later (it expires
  later): never back to the old one.
A roll session is the first session on a new front contract. Footprint's trading day of
that session is the same date (an overnight session belongs to the day it leads into).
The output is the backtest's --rolls file: a `session` header, one date per line.
Read-only on the source; refuses to overwrite an existing output.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
SESSION_SHIFT = timedelta(hours=6)  # 18:00 New York + 6 h = midnight: the next date


def _session(ts_event: str) -> date:
    moment = datetime.fromisoformat(ts_event[:19] + "+00:00")
    return (moment.astimezone(NEW_YORK) + SESSION_SHIFT).date()


def roll_sessions(source: Path) -> list[date]:
    """Every roll session in `source`, in order."""
    volume: dict[tuple[date, int], int] = defaultdict(int)
    last_seen: dict[int, str] = {}
    with source.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if "-" in row["symbol"]:
                continue
            contract = int(row["instrument_id"])
            volume[(_session(row["ts_event"]), contract)] += int(row["volume"])
            if row["ts_event"] > last_seen.get(contract, ""):
                last_seen[contract] = row["ts_event"]
    by_session: dict[date, list[tuple[int, int]]] = defaultdict(list)
    for (session, contract), lots in volume.items():
        by_session[session].append((lots, contract))
    rolls: list[date] = []
    front: int | None = None
    for session in sorted(by_session):
        _, leader = max(by_session[session])
        if front is None:
            front = leader
        elif last_seen[leader] > last_seen[front]:
            front = leader
            rolls.append(session)
    return rolls


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/build_rolls.py")
    parser.add_argument("source", type=Path, help="Databento OHLCV-1m CSV (decompressed)")
    parser.add_argument("out", type=Path, help="the roll file to write (must not exist)")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"{args.out} exists: refusing to overwrite it")
    sessions = roll_sessions(args.source)
    args.out.write_text(
        "session\n" + "".join(f"{d.isoformat()}\n" for d in sessions), encoding="utf-8"
    )
    print(f"{len(sessions)} roll sessions written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
