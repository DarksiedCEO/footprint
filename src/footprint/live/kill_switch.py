"""Andre's operator command for the durable kill switch (P6-1, decided 2026-10-10).

    uv run python -m footprint.live.kill_switch status
    uv run python -m footprint.live.kill_switch clear --by Andre --reason "..."
    uv run python -m footprint.live.kill_switch clear --by Andre --reason "..." --reset-peak

Stop the live system first. The command opens the ledger exclusively, so it is refused
while the live system has it open (a running system would overwrite the clear). It
refuses to clear while a position is open or a CHECK THE FILL hold stands: look in
IBKR's own platform and book or flatten by hand first. It asks you to type CLEAR.

Clearing keeps the drawdown peak. After a drawdown latch the drawdown limit is then
still breached and entries stay blocked; `--reset-peak` starts the drawdown count again
from today's equity, and only when you ask for it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from footprint.core.config import Profiles, load_profiles
from footprint.decision.risk.gate import drawdown_breached
from footprint.execution.ledger import Ledger, LedgerBusy, StateStore

CONFIRM_WORD = "CLEAR"
OK = 0
REFUSED = 2


def _status(profiles: Profiles, ledger: Ledger, say: Callable[[str], None]) -> None:
    state = ledger.account(datetime.now(UTC))
    say(f"kill switch: {'LATCHED' if state.kill_switch_latched else 'not latched'}")
    say(f"equity: {state.equity}   drawdown peak: {state.peak_equity}")
    breached = drawdown_breached(profiles.risk, state)
    say(f"drawdown limit: {'breached' if breached else 'not breached'}")
    say(f"open position: {ledger.open_position_id or 'none'}")
    for hold in ledger.sold_unpriced:
        say(
            f"CHECK THE FILL hold: {hold.order_id} sold {hold.contract_id} "
            f"(position {hold.position_id}), price never booked"
        )


def _blockers(ledger: Ledger) -> list[str]:
    found: list[str] = []
    if ledger.open_position_id is not None:
        found.append(
            f"position {ledger.open_position_id} is open: close it, or book it by hand, first"
        )
    found.extend(
        f"CHECK THE FILL hold on {hold.contract_id} (order {hold.order_id}): check the fill "
        "in IBKR and book it by hand first"
        for hold in ledger.sold_unpriced
    )
    return found


def _clear(
    profiles: Profiles,
    ledger: Ledger,
    args: argparse.Namespace,
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> int:
    if not ledger.account(datetime.now(UTC)).kill_switch_latched:
        say("The kill switch is not latched: nothing to clear.")
        return OK
    if not args.by.strip() or not args.reason.strip():
        say("Refused: --by (your name) and --reason are both required.")
        return REFUSED
    blockers = _blockers(ledger)
    if blockers:
        say("Refused, nothing changed:")
        for line in blockers:
            say(f"  - {line}")
        return REFUSED
    _status(profiles, ledger, say)
    peak_note = "and RESET the drawdown peak" if args.reset_peak else "keeping the drawdown peak"
    answer = ask(f"Clear the kill switch {peak_note}? Type {CONFIRM_WORD} to confirm: ")
    if answer.strip() != CONFIRM_WORD:
        say("Kill switch not cleared.")
        return REFUSED
    ledger.clear_kill_switch(args.by, reason=args.reason, reset_peak=args.reset_peak)
    say(f"Kill switch cleared by {args.by.strip()}.")
    if drawdown_breached(profiles.risk, ledger.account(datetime.now(UTC))):
        say(
            "The drawdown limit is still breached, so entries stay blocked. Run again "
            "with --reset-peak only if you mean to start the drawdown count fresh."
        )
    return OK


def main(
    argv: list[str] | None = None,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.live.kill_switch")
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    parser.add_argument("--state", type=Path, default=Path("state"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show the kill switch, drawdown and holds")
    clear = commands.add_parser("clear", help="clear a latched kill switch")
    clear.add_argument("--by", required=True, help="your name")
    clear.add_argument("--reason", required=True, help="why it is safe to clear")
    clear.add_argument(
        "--reset-peak",
        action="store_true",
        help="also reset the drawdown peak to current equity (forgives the loss so far)",
    )
    args = parser.parse_args(argv)
    profiles = load_profiles(args.config, args.instrument)
    store = StateStore(args.state / "ledger.json")
    try:
        ledger = Ledger(profiles.risk, profiles.instrument, store, exclusive=True)
    except LedgerBusy:
        say("Refused: the ledger is in use. stop the live system first, then run this again.")
        return REFUSED
    except FileNotFoundError:
        say(f"Refused: there is no ledger at {store.path}; nothing was created.")
        return REFUSED
    try:
        if args.command == "status":
            _status(profiles, ledger, say)
            return OK
        return _clear(profiles, ledger, args, ask, say)
    finally:
        ledger.close()


if __name__ == "__main__":
    sys.exit(main())
