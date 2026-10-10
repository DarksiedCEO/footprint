"""Record the option chain MarketData holds to dated CSV files (Q3), for calibrating
D8 (minimum open interest) and D22 (the backtest's option model).

    uv run python -m footprint.brokers.ibkr.record_chain --out data/chain/ [--minutes N]

Read-only: it connects exactly as `check` does (same .env, same paper-only settings,
the same read-only gateway that refuses to place, change or cancel any order), waits for
the resync, then hands the MarketData service the runner reads to
`chain_recorder.record_chain`: one snapshot every `market_data.record_interval_seconds`
for `market_data.record_minutes` (or `--minutes N`), appended to one file per UTC day in
`--out` (created if missing). Only fresh quotes are written, each with its own age; a
disconnect or no fresh chain writes nothing for that interval and the recording goes on.

Exit code 0 when at least one snapshot was written and none was refused or failed;
1 otherwise (not resynced, `--out` not a directory, nothing recorded, an ERROR line);
NOT_PAPER_EXIT (as `check`) when the account in .env is not a paper account.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

try:
    from footprint.brokers.ibkr.binding import IbConnectionSettings, read_env_file
except ModuleNotFoundError as missing:  # pragma: no cover - only without ibapi
    if missing.name != "ibapi" and not (missing.name or "").startswith("ibapi."):
        raise
    sys.exit("ibapi is not installed: run  uv run python scripts/install_ibapi.py")
from footprint.brokers.ibkr.chain_recorder import record_chain, run_seconds
from footprint.brokers.ibkr.check import NOT_PAPER_EXIT, check
from footprint.brokers.ibkr.market import MarketData
from footprint.brokers.ibkr.paper import LiveAccountRefused
from footprint.core.config import load_profiles


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(
    argv: list[str] | None = None,
    run_check: Callable[..., int] = check,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.brokers.ibkr.record_chain")
    parser.add_argument("--out", type=Path, required=True, help="the directory for the day files")
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="how long to record (default: market_data.record_minutes in config)",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    parser.add_argument(
        "--wait",
        type=float,
        default=None,
        help="seconds to wait for the resync (default: one reconnect plus the resync limit)",
    )
    args = parser.parse_args(argv)
    try:
        profiles = load_profiles(args.config, args.instrument)
        m = profiles.execution.market_data
        length = run_seconds(m.record_minutes, args.minutes)
    except ValueError as exc:
        parser.error(str(exc))
    out: Path = args.out
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # FileExistsError included: a file where the directory goes
        print(f"REFUSED: {out}: {exc}")
        return 1
    ibkr = profiles.execution.ibkr
    try:
        settings = IbConnectionSettings.from_env(read_env_file(str(args.env)), ibkr)
    except LiveAccountRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return NOT_PAPER_EXIT
    wait = args.wait
    if wait is None:
        wait = float(ibkr.reconnect_seconds + ibkr.positions_disagree_alarm_seconds)
    max_age = profiles.execution.quotes.max_age_seconds

    def record(market: MarketData, say: Callable[[str], None], poll_seconds: float) -> int:
        say(
            f"recording the chain every {m.record_interval_seconds} s for {length} s "
            f"into {out} (quotes no older than {max_age} s)"
        )
        return record_chain(
            market,
            out,
            interval_seconds=m.record_interval_seconds,
            run_seconds=length,
            max_age_seconds=max_age,
            poll_seconds=poll_seconds,
            say=say,
            clock=clock,
            sleep=sleep,
        )

    return run_check(
        profiles, settings, wait, sys.stdout, clock=clock, sleep=sleep, market_action=record
    )


if __name__ == "__main__":
    sys.exit(main())
