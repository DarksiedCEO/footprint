"""Clearing the kill switch (P6-1, decided by Andre on 2026-10-10).

(a) An operator command, `python -m footprint.live.kill_switch`, clears it: it needs a
name and a reason, asks for a typed confirmation, and refuses while a position is
open, while a CHECK THE FILL hold stands, or while the live system has the ledger open.
(b) Clearing keeps the drawdown peak unless Andre asks for a reset (`--reset-peak`).
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
from footprint.decision.risk.gate import drawdown_breached
from footprint.execution.ledger import Ledger, LedgerBusy, SoldHold, StateStore
from footprint.live import kill_switch
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def _latched_by_drawdown(profiles: Profiles, path: Path) -> Ledger:
    ledger = Ledger(profiles.risk, profiles.instrument, StateStore(path), create=True)
    ledger.on_entry_filled(T0, "a", SessionKind.PRIME)
    ledger.on_trade_closed(T0, "a", -profiles.risk.bankroll * D("0.2"))
    assert ledger.account(T0).kill_switch_latched
    return ledger


def _latched_by_a_halt(profiles: Profiles, path: Path) -> Ledger:
    ledger = Ledger(profiles.risk, profiles.instrument, StateStore(path), create=True)
    ledger.latch_kill_switch("an order that will not cancel")
    return ledger


# --- (b) the ledger keeps the peak unless asked -------------------------------------


def test_clearing_keeps_the_drawdown_peak_by_default(profiles: Profiles, tmp_path: Path) -> None:
    ledger = _latched_by_drawdown(profiles, tmp_path / "l.json")
    peak = ledger.account(T0).peak_equity
    ledger.clear_kill_switch("Andre", reason="looked at it")
    state = ledger.account(T0)
    assert not state.kill_switch_latched
    assert state.peak_equity == peak
    assert drawdown_breached(profiles.risk, state)  # the loss is not forgiven


def test_reset_peak_is_only_by_request(profiles: Profiles, tmp_path: Path) -> None:
    ledger = _latched_by_drawdown(profiles, tmp_path / "l.json")
    ledger.clear_kill_switch("Andre", reason="start fresh", reset_peak=True)
    state = ledger.account(T0)
    assert state.peak_equity == state.equity
    assert not drawdown_breached(profiles.risk, state)


def test_a_clear_is_logged_with_name_and_reason(
    profiles: Profiles, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = _latched_by_a_halt(profiles, tmp_path / "l.json")
    with caplog.at_level(logging.CRITICAL, logger="footprint.ledger"):
        ledger.clear_kill_switch("Andre", reason="checked IBKR, flat")
    text = caplog.text
    assert "Andre" in text
    assert "checked IBKR, flat" in text
    assert "peak kept" in text


# --- the ledger file is held by one process at a time ------------------------------


def test_an_exclusive_ledger_refuses_a_second_exclusive_open(
    profiles: Profiles, tmp_path: Path
) -> None:
    path = tmp_path / "l.json"
    first = Ledger(
        profiles.risk, profiles.instrument, StateStore(path), create=True, exclusive=True
    )
    with pytest.raises(LedgerBusy, match="in use"):
        Ledger(profiles.risk, profiles.instrument, StateStore(path), exclusive=True)
    first.close()
    second = Ledger(profiles.risk, profiles.instrument, StateStore(path), exclusive=True)
    second.close()


def test_a_non_exclusive_open_is_unchanged(profiles: Profiles, tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    first = Ledger(
        profiles.risk, profiles.instrument, StateStore(path), create=True, exclusive=True
    )
    Ledger(profiles.risk, profiles.instrument, StateStore(path))  # no lock asked: opens
    first.close()


# --- (a) the operator command --------------------------------------------------------


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


def _reload(profiles: Profiles, state: Path) -> Ledger:
    return Ledger(profiles.risk, profiles.instrument, StateStore(state / "ledger.json"))


def test_status_says_latched_and_why_entries_stay_blocked(
    profiles: Profiles, tmp_path: Path
) -> None:
    _latched_by_drawdown(profiles, tmp_path / "ledger.json")
    code, term = _run(tmp_path, ["status"], [])
    assert code == 0
    assert "LATCHED" in term.text
    assert "drawdown limit: breached" in term.text


def test_clear_with_confirmation_keeps_the_peak_and_says_so(
    profiles: Profiles, tmp_path: Path
) -> None:
    _latched_by_drawdown(profiles, tmp_path / "ledger.json")
    code, term = _run(
        tmp_path, ["clear", "--by", "Andre", "--reason", "reviewed the loss"], ["CLEAR"]
    )
    assert code == 0
    reloaded = _reload(profiles, tmp_path).account(T0)
    assert not reloaded.kill_switch_latched
    assert drawdown_breached(profiles.risk, reloaded)
    assert "--reset-peak" in term.text  # tells Andre entries stay blocked and why


def test_clear_with_reset_peak(profiles: Profiles, tmp_path: Path) -> None:
    _latched_by_drawdown(profiles, tmp_path / "ledger.json")
    code, _ = _run(
        tmp_path,
        ["clear", "--by", "Andre", "--reason", "fresh start", "--reset-peak"],
        ["CLEAR"],
    )
    assert code == 0
    reloaded = _reload(profiles, tmp_path).account(T0)
    assert not reloaded.kill_switch_latched
    assert reloaded.peak_equity == reloaded.equity


def test_a_wrong_confirmation_changes_nothing(profiles: Profiles, tmp_path: Path) -> None:
    _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    code, term = _run(tmp_path, ["clear", "--by", "Andre", "--reason", "x"], ["clear"])
    assert code == 2
    assert "not cleared" in term.text
    assert _reload(profiles, tmp_path).account(T0).kill_switch_latched


@pytest.mark.parametrize(("by", "reason"), [(" ", "a reason"), ("Andre", " ")])
def test_a_name_and_a_reason_are_required(
    profiles: Profiles, tmp_path: Path, by: str, reason: str
) -> None:
    _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    code, _ = _run(tmp_path, ["clear", "--by", by, "--reason", reason], ["CLEAR"])
    assert code == 2
    assert _reload(profiles, tmp_path).account(T0).kill_switch_latched


def test_refused_while_a_position_is_open(profiles: Profiles, tmp_path: Path) -> None:
    ledger = _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    ledger.on_entry_filled(T0, "open-1", SessionKind.PRIME)
    code, term = _run(tmp_path, ["clear", "--by", "Andre", "--reason", "x"], ["CLEAR"])
    assert code == 2
    assert "open-1" in term.text
    assert _reload(profiles, tmp_path).account(T0).kill_switch_latched


def test_refused_while_a_check_the_fill_hold_stands(profiles: Profiles, tmp_path: Path) -> None:
    ledger = _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    ledger.hold_sold(SoldHold("gone-1", "C-1", "exit-1"))
    code, term = _run(tmp_path, ["clear", "--by", "Andre", "--reason", "x"], ["CLEAR"])
    assert code == 2
    assert "CHECK THE FILL" in term.text
    assert "exit-1" in term.text
    assert _reload(profiles, tmp_path).account(T0).kill_switch_latched


def test_refused_while_the_live_system_has_the_ledger(profiles: Profiles, tmp_path: Path) -> None:
    _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    live = Ledger(
        profiles.risk, profiles.instrument, StateStore(tmp_path / "ledger.json"), exclusive=True
    )
    code, term = _run(tmp_path, ["clear", "--by", "Andre", "--reason", "x"], ["CLEAR"])
    assert code == 2
    assert "stop the live system" in term.text
    live.close()
    assert _reload(profiles, tmp_path).account(T0).kill_switch_latched


def test_nothing_to_clear(profiles: Profiles, tmp_path: Path) -> None:
    Ledger(profiles.risk, profiles.instrument, StateStore(tmp_path / "ledger.json"), create=True)
    code, term = _run(tmp_path, ["clear", "--by", "Andre", "--reason", "x"], [])
    assert code == 0
    assert "not latched" in term.text


def test_a_missing_ledger_is_never_created(tmp_path: Path) -> None:
    code, term = _run(tmp_path, ["status"], [])
    assert code == 2
    assert "no ledger" in term.text
    assert not (tmp_path / "ledger.json").exists()


# --- grind review G24-1..G24-3 (2026-10-10): pin what reviewer probes left open ------


@pytest.mark.parametrize("answer", ["don't CLEAR", "CLEAR it", "CLEARED", "yes CLEAR"])
def test_only_the_exact_word_confirms(profiles: Profiles, tmp_path: Path, answer: str) -> None:
    """G24-1: a near-miss that contains the word is not a confirmation."""
    _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    code, term = _run(tmp_path, ["clear", "--by", "Andre", "--reason", "x"], [answer])
    assert code == 2
    assert "not cleared" in term.text
    assert _reload(profiles, tmp_path).account(T0).kill_switch_latched


def test_the_command_releases_the_ledger_when_it_returns(
    profiles: Profiles, tmp_path: Path
) -> None:
    """G24-2: after the command returns, the live system (or the command again) can
    take the ledger in the same process."""
    _latched_by_a_halt(profiles, tmp_path / "ledger.json")
    code, _ = _run(tmp_path, ["status"], [])
    assert code == 0
    again = Ledger(
        profiles.risk, profiles.instrument, StateStore(tmp_path / "ledger.json"), exclusive=True
    )
    again.close()


def test_a_missing_ledger_releases_its_lock(profiles: Profiles, tmp_path: Path) -> None:
    """G24-3: refusing a missing ledger does not leave the lock taken."""
    store = StateStore(tmp_path / "ledger.json")
    with pytest.raises(FileNotFoundError):
        Ledger(profiles.risk, profiles.instrument, store, exclusive=True)
    first = Ledger(profiles.risk, profiles.instrument, store, create=True, exclusive=True)
    first.close()
