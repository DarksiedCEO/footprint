"""Start the system on IBKR's paper account.

    uv run python -m footprint.live --blackouts news.csv
    uv run python -m footprint.live --no-blackouts          # explicitly no news calendar
    uv run python -m footprint.live --new-ledger ...        # the very first run only

Reads .env (BROKER_HOST, BROKER_PORT, BROKER_ACCOUNT_ID, BROKER_CLIENT_ID) and config/.
Keeps its ledger, journal and alarm log in --state (default: state/). Type at the
prompt: `clear <name>` to allow entries, `status`, `quit` (or Ctrl-C).
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
from pathlib import Path

from footprint.brokers.ibkr.binding import IbConnectionSettings, read_env_file
from footprint.core.config import load_profiles
from footprint.data.news import blackouts_from
from footprint.live.app import AlarmFile, Driver, build


def _read_commands(commands: queue.Queue[str], stop: threading.Event) -> None:
    for line in sys.stdin:
        commands.put(line)
        if stop.is_set():
            return
    commands.put("quit")  # stdin closed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.live")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    parser.add_argument("--state", type=Path, default=Path("state"))
    news = parser.add_mutually_exclusive_group(required=True)
    news.add_argument("--blackouts", type=Path, help="news blackouts CSV (name,start,end)")
    news.add_argument(
        "--no-blackouts", action="store_true", help="run with no news calendar (no blackouts)"
    )
    parser.add_argument(
        "--new-ledger", action="store_true", help="first run only: create the ledger file"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    profiles = load_profiles(args.config, args.instrument)
    settings = IbConnectionSettings.from_env(read_env_file(str(args.env)), profiles.execution.ibkr)
    live = build(
        profiles,
        settings,
        args.state,
        AlarmFile(args.state / "alarms.log"),
        blackouts_from(args.blackouts),
        new_ledger=args.new_ledger,
    )
    stop = threading.Event()
    commands: queue.Queue[str] = queue.Queue()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    reader = threading.Thread(target=_read_commands, args=(commands, stop), daemon=True)
    reader.start()
    try:
        Driver(live).run(stop, commands)
    except KeyboardInterrupt:
        stop.set()
    return 1 if live.binding.refused is not None else 0


if __name__ == "__main__":
    sys.exit(main())
