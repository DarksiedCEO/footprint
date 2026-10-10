"""Booking a position by hand (G24-4, decided by Andre on 2026-10-10).

`python -m footprint.live.kill_switch book --position ID --pnl AMOUNT --by NAME
--reason TEXT [--unrecorded]` books a position at the net P&L Andre read in IBKR's
statement, through `Ledger.book_by_hand`. Same guards as `clear`: the live system must
be stopped (exclusive ledger), a name and a reason are required, and Andre types BOOK.
It refuses an id the ledger does not know (the open position or a CHECK THE FILL hold)
unless `--unrecorded` says it is a trade the ledger never recorded. It never clears the
kill switch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from footprint.core.calendar import SessionKind
from footprint.core.config import Profiles
from footprint.execution.ledger import Ledger, SoldHold, StateStore
from footprint.live import kill_switch
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
LOSS = "-412.50"


class Terminal:
    def __init__(self, answers: list[str]) -> None:
        self._answers: Iterator[str] = iter(answers)
        self.lines: list[str] = []

    def ask(self, prompt: str) -> str:
        self.lines.append(prompt)
        return next(self._answers)

    def say(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _run(state: Path, args: list[str], answers: list[str]) -> tuple[int, Terminal]:
    term = Terminal(answers)
    code = kill_switch.main(
        ["--config", str(REPO_ROOT / "config"), "--state", str(state), *args],
        ask=term.ask,
        say=term.say,
    )
    return code, term


def _book_args(position: str, pnl: str = LOSS, *extra: str) -> list[str]:
    return [
        "book",
        "--position",
        position,
        f"--pnl={pnl}",
        "--by",
        "Andre",
        "--reason",
        "IBKR statement",
        *extra,
    ]


def _ledger(profiles: Profiles, state: Path, *, create: bool = False) -> Ledger:
    return Ledger(
        profiles.risk, profiles.instrument, StateStore(state / "ledger.json"), create=create
    )


def _open_and_latched(profiles: Profiles, state: Path, position: str = "pos-1") -> Ledger:
    ledger = _ledger(profiles, state, create=True)
    ledger.on_entry_filled(T0, position, SessionKind.PRIME)
    ledger.latch_kill_switch("an order that will not cancel")
    return ledger


def _snapshot(profiles: Profiles, state: Path) -> str:
    """The ledger file after today's day/week roll (any open rolls it; that is not a
    booking), so a refusal must leave it byte for byte unchanged."""
    _ledger(profiles, state).account(datetime.now(UTC))
    return (state / "ledger.json").read_text()


# --- what a booking does ------------------------------------------------------------


def test_books_the_open_position_at_the_given_pnl(profiles: Profiles, tmp_path: Path) -> None:
    before = _open_and_latched(profiles, tmp_path).account(T0).equity
    code, term = _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    assert code == 0
    after = _ledger(profiles, tmp_path)
    assert after.account(T0).equity == before + D(LOSS)
    assert after.open_position_id is None
    assert "booked" in term.text


def test_booking_never_clears_the_kill_switch(profiles: Profiles, tmp_path: Path) -> None:
    _open_and_latched(profiles, tmp_path)
    code, term = _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    assert code == 0
    assert _ledger(profiles, tmp_path).account(T0).kill_switch_latched
    assert "kill switch is still LATCHED" in term.text


def test_booking_releases_the_check_the_fill_hold(profiles: Profiles, tmp_path: Path) -> None:
    ledger = _open_and_latched(profiles, tmp_path)
    ledger.hold_sold(SoldHold("pos-1", "C-1", "exit-1"))
    code, _ = _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    assert code == 0
    assert _ledger(profiles, tmp_path).sold_unpriced == ()


def test_a_hold_on_a_position_no_longer_open_can_be_booked(
    profiles: Profiles, tmp_path: Path
) -> None:
    ledger = _ledger(profiles, tmp_path, create=True)
    ledger.hold_sold(SoldHold("gone-1", "C-1", "exit-1"))
    before = ledger.account(T0).equity
    code, _ = _run(tmp_path, _book_args("gone-1"), ["BOOK"])
    assert code == 0
    after = _ledger(profiles, tmp_path)
    assert after.sold_unpriced == ()
    assert after.account(T0).equity == before + D(LOSS)


def test_the_screen_shows_equity_before_and_after(profiles: Profiles, tmp_path: Path) -> None:
    before = _open_and_latched(profiles, tmp_path).account(T0).equity
    _, term = _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    assert str(before) in term.text
    assert str(before + D(LOSS)) in term.text


def test_the_booking_is_logged_with_name_and_reason(
    profiles: Profiles, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _open_and_latched(profiles, tmp_path)
    with caplog.at_level(logging.CRITICAL, logger="footprint.ledger"):
        _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    assert "Andre" in caplog.text
    assert "IBKR statement" in caplog.text
    assert "pos-1" in caplog.text


def test_an_unrecorded_trade_needs_the_flag(profiles: Profiles, tmp_path: Path) -> None:
    ledger = _ledger(profiles, tmp_path, create=True)
    before = ledger.account(T0).equity
    code, _ = _run(tmp_path, _book_args("never-seen", LOSS, "--unrecorded"), ["BOOK"])
    assert code == 0
    assert _ledger(profiles, tmp_path).account(T0).equity == before + D(LOSS)


# --- refusals: nothing changes --------------------------------------------------------


def test_an_unknown_position_is_refused_without_the_flag(
    profiles: Profiles, tmp_path: Path
) -> None:
    _open_and_latched(profiles, tmp_path)
    before = _snapshot(profiles, tmp_path)
    code, term = _run(tmp_path, _book_args("pos-typo"), ["BOOK"])
    assert code == 2
    assert "--unrecorded" in term.text
    assert _snapshot(profiles, tmp_path) == before


@pytest.mark.parametrize("answer", ["book", "don't BOOK", "BOOKED", ""])
def test_only_the_exact_word_confirms(profiles: Profiles, tmp_path: Path, answer: str) -> None:
    _open_and_latched(profiles, tmp_path)
    before = _snapshot(profiles, tmp_path)
    code, term = _run(tmp_path, _book_args("pos-1"), [answer])
    assert code == 2
    assert "not booked" in term.text
    assert _snapshot(profiles, tmp_path) == before


@pytest.mark.parametrize("pnl", ["abc", "NaN", "Infinity", "-inf", "1,000", "", "sNaN"])
def test_a_pnl_that_is_not_a_finite_number_is_refused(
    profiles: Profiles, tmp_path: Path, pnl: str
) -> None:
    _open_and_latched(profiles, tmp_path)
    before = _snapshot(profiles, tmp_path)
    code, term = _run(tmp_path, _book_args("pos-1", pnl), ["BOOK"])
    assert code == 2
    assert "--pnl" in term.text
    assert _snapshot(profiles, tmp_path) == before


@pytest.mark.parametrize(("by", "reason"), [(" ", "a reason"), ("Andre", " ")])
def test_a_name_and_a_reason_are_required(
    profiles: Profiles, tmp_path: Path, by: str, reason: str
) -> None:
    _open_and_latched(profiles, tmp_path)
    before = _snapshot(profiles, tmp_path)
    args = ["book", "--position", "pos-1", "--pnl", LOSS, "--by", by, "--reason", reason]
    code, _ = _run(tmp_path, args, ["BOOK"])
    assert code == 2
    assert _snapshot(profiles, tmp_path) == before


def test_a_second_booking_of_the_same_id_is_refused(profiles: Profiles, tmp_path: Path) -> None:
    _ledger(profiles, tmp_path, create=True)
    first, _ = _run(tmp_path, _book_args("x-1", LOSS, "--unrecorded"), ["BOOK"])
    assert first == 0
    before = _snapshot(profiles, tmp_path)
    code, term = _run(tmp_path, _book_args("x-1", LOSS, "--unrecorded"), ["BOOK"])
    assert code == 2
    assert "already booked" in term.text
    assert _snapshot(profiles, tmp_path) == before


def test_refused_while_the_live_system_has_the_ledger(profiles: Profiles, tmp_path: Path) -> None:
    _open_and_latched(profiles, tmp_path)
    live = Ledger(
        profiles.risk, profiles.instrument, StateStore(tmp_path / "ledger.json"), exclusive=True
    )
    try:
        code, term = _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    finally:
        live.close()
    assert code == 2
    assert "stop the live system" in term.text
    assert _ledger(profiles, tmp_path).open_position_id == "pos-1"


def test_a_missing_ledger_is_never_created(tmp_path: Path) -> None:
    code, term = _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    assert code == 2
    assert "no ledger" in term.text
    assert not (tmp_path / "ledger.json").exists()


def test_the_command_releases_the_ledger_after_booking(profiles: Profiles, tmp_path: Path) -> None:
    _open_and_latched(profiles, tmp_path)
    _run(tmp_path, _book_args("pos-1"), ["BOOK"])
    again = Ledger(
        profiles.risk, profiles.instrument, StateStore(tmp_path / "ledger.json"), exclusive=True
    )
    again.close()


def test_a_negative_pnl_can_be_typed_as_its_own_argument(
    profiles: Profiles, tmp_path: Path
) -> None:
    """How the runbook shows it: `--pnl -412.50`, not `--pnl=-412.50`."""
    before = _open_and_latched(profiles, tmp_path).account(T0).equity
    args = ["book", "--position", "pos-1", "--pnl", LOSS, "--by", "Andre", "--reason", "stmt"]
    code, _ = _run(tmp_path, args, ["BOOK"])
    assert code == 0
    assert _ledger(profiles, tmp_path).account(T0).equity == before + D(LOSS)
