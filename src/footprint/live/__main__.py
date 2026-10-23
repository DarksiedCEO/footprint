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

from footprint.brokers.ibkr.paper import LiveAccountRefused
from footprint.brokers.ibkr.settings import IbConnectionSettings, read_env_file
from footprint.core.config import load_profiles
from footprint.data.news import blackouts_from
from footprint.decision.play import PlayHistoryTooShort
from footprint.execution.channels import ChannelSettings, TelegramOnlySettings
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
    degraded = parser.add_mutually_exclusive_group()
    degraded.add_argument(
        "--alarms-local-only",
        action="store_true",
        help="DEGRADED_ALERTING, no phone alarms: the terminal and state/alarms.log only "
        "(attended at the terminal only)",
    )
    degraded.add_argument(
        "--alarms-telegram-only",
        action="store_true",
        help="DEGRADED_ALERTING: Telegram only, no SMS fallback, no calls (attended at the "
        "terminal only)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    profiles = load_profiles(args.config, args.instrument)
    env = read_env_file(str(args.env))
    try:
        settings = IbConnectionSettings.from_env(env, profiles.execution.ibkr)
    except LiveAccountRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    channels: ChannelSettings | TelegramOnlySettings | None = None
    if (args.alarms_local_only or args.alarms_telegram_only) and not sys.stdin.isatty():
        print(
            "REFUSED: DEGRADED_ALERTING needs a person at an interactive terminal "
            "(unattended operation is blocked without full alarms)",
            file=sys.stderr,
        )
        return 2
    try:
        if args.alarms_local_only:
            print("DEGRADED_ALERTING: local only, nothing reaches your phone", file=sys.stderr)
        elif args.alarms_telegram_only:
            channels = TelegramOnlySettings.from_env(env)
            print("DEGRADED_ALERTING: Telegram only (no SMS, no calls)", file=sys.stderr)
        else:
            channels = ChannelSettings.from_env(env)
    except ValueError as exc:
        print(
            f"phone alarms are not set up ({exc}). Fill them in (runbook), or start "
            "degraded with --alarms-telegram-only or --alarms-local-only",
            file=sys.stderr,
        )
        return 2
    try:
        live = build(
            profiles,
            settings,
            args.state,
            AlarmFile(args.state / "alarms.log"),
            blackouts_from(args.blackouts),
            channels=channels,
            new_ledger=args.new_ledger,
        )
    except PlayHistoryTooShort as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
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
