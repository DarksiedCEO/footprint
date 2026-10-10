"""Export IBKR's 1-minute history of the traded future to the CSV the backtest reads.

    uv run python -m footprint.brokers.ibkr.history --out data/nq_1m.csv [--days N]
    uv run python -m footprint.backtest --bars data/nq_1m.csv --out runs/first

Read-only: it connects exactly as `check` does (same .env, same paper-only settings,
the same read-only gateway that refuses to place, change or cancel any order), waits for
the resync, then lets the MarketData service the runner reads load the history (front
future, spliced and back-adjusted across rolls, Saturdays skipped, requests paced from
config) and hands it to `history_export.export_history`: an existing file is never
overwritten, a history with a gap is refused, and nothing appears under the finished
name unless every bar was written.

`--days N` loads N days instead of `market_data.history_days` for this run only.

Exit code 0 when the file was written; 1 when it was not (not resynced, refused, no
history in time, a gap, a write failure); NOT_PAPER_EXIT (as `check`) when the account
in .env is not a paper account.
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
from footprint.brokers.ibkr.check import NOT_PAPER_EXIT, check
from footprint.brokers.ibkr.history_export import export_history, window_start, with_days
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
    parser = argparse.ArgumentParser(prog="python -m footprint.brokers.ibkr.history")
    parser.add_argument("--out", type=Path, required=True, help="the CSV to write (must not exist)")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="days of history to export (default: market_data.history_days in config)",
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
        profiles = with_days(load_profiles(args.config, args.instrument), args.days)
    except ValueError as exc:
        parser.error(str(exc))
    ibkr = profiles.execution.ibkr
    try:
        settings = IbConnectionSettings.from_env(read_env_file(str(args.env)), ibkr)
    except LiveAccountRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return NOT_PAPER_EXIT
    wait = args.wait
    if wait is None:
        wait = float(ibkr.reconnect_seconds + ibkr.positions_disagree_alarm_seconds)
    m = profiles.execution.market_data
    out: Path = args.out

    def window_from(now: datetime) -> datetime:
        return window_start(now, m.history_days)

    def export(market: MarketData, say: Callable[[str], None], poll_seconds: float) -> int:
        say(
            f"history: {m.history_days} days from {window_from(clock()).isoformat()} "
            f"(00:00 UTC on the day it finishes loading, less {m.history_days} days); "
            f"waiting up to {m.export_wait_seconds} s for it to load"
        )
        return export_history(
            market,
            out,
            window_from,
            profiles.instrument,
            m.export_wait_seconds,
            m.export_max_missing_minutes,
            poll_seconds,
            say,
            clock,
            sleep,
        )

    return run_check(
        profiles, settings, wait, sys.stdout, clock=clock, sleep=sleep, market_action=export
    )


if __name__ == "__main__":
    sys.exit(main())
