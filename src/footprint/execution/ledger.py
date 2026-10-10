"""The account ledger: the risk layer's view of the account, kept durable.

- A stop-out is ANY trade closed at a net loss (G6); P&L is always net of fees (G15).
- Daily counters reset at the prime-session start of a new day (so a tripped daily
  breaker keeps the swarm dark through the overnight session until the next prime
  open); weekly counters reset with the first day of a new ISO week.
- The kill switch latches when equity falls the configured percentage from its
  peak, and on every operational halt in management and reconcile (an order that will
  not cancel, a disagreement with the broker). It survives restarts; only
  `clear_kill_switch` (Andre, by hand, through `python -m footprint.live.kill_switch`)
  unlatches it. Clearing keeps the drawdown peak unless Andre asks for a reset (P6-1,
  decided 2026-10-10), so a loss is never forgiven by accident.
- An exclusive ledger (`exclusive=True`: the live system and the operator command)
  holds an OS lock on a sibling `.lock` file for as long as it is open, so a clear made
  while the live system runs is refused instead of being overwritten by it.
- A sell of the open position that the broker reports FILLED with its price pending
  is held durably (`hold_sold`, grind review G2-1, D40): reconcile never sells that
  contract again, also after a restart. The hold is released only when the sale is
  booked at its real price (`on_trade_closed`) or Andre books the position by hand;
  clearing the kill switch does not release it.
- Every change is written to disk before the call returns (atomic replace + fsync),
  so a crash never forgets a breaker, a latch or an open position.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from footprint.core.calendar import SessionKind
from footprint.core.config import InstrumentProfile, RiskProfile
from footprint.core.models import require_utc
from footprint.decision.risk.gate import AccountState, drawdown_breached

log = logging.getLogger("footprint.ledger")


class LedgerBusy(RuntimeError):
    """Another process holds this ledger exclusively (the live system is running)."""


@dataclass(frozen=True, slots=True)
class SoldHold:
    """A sell of `position_id` on `contract_id` the broker reported FILLED with its
    price pending (D39, D40): that contract is already sold and is never sold again."""

    position_id: str
    contract_id: str
    order_id: str


@dataclass(frozen=True, slots=True)
class LedgerState:
    equity: Decimal
    peak_equity: Decimal
    realized_today: Decimal
    realized_week: Decimal
    stopouts_today: int
    trades_today_prime: int
    trades_today_overnight: int
    open_position_id: str | None
    kill_switch_latched: bool
    day: date | None
    week: tuple[int, int] | None
    booked_by_hand: tuple[str, ...] = ()
    # The newest completed bar a decision was made on: a restart never decides it again.
    last_decided: datetime | None = None
    # Filled sells whose price is pending (D40): kept until the sale is booked.
    sold_unpriced: tuple[SoldHold, ...] = ()


class StateStore:
    """One JSON file, replaced atomically on every save."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def lock(self) -> int:
        """Take the ledger's exclusive OS lock without waiting; returns the open file
        descriptor that holds it. Raises LedgerBusy when another holder has it."""
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LedgerBusy(f"ledger {self._path} is in use by another process") from None
        return fd

    def load(self) -> LedgerState | None:
        if not self._path.exists():
            return None
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return LedgerState(
            equity=Decimal(raw["equity"]),
            peak_equity=Decimal(raw["peak_equity"]),
            realized_today=Decimal(raw["realized_today"]),
            realized_week=Decimal(raw["realized_week"]),
            stopouts_today=int(raw["stopouts_today"]),
            trades_today_prime=int(raw["trades_today_prime"]),
            trades_today_overnight=int(raw["trades_today_overnight"]),
            open_position_id=raw["open_position_id"],
            kill_switch_latched=bool(raw["kill_switch_latched"]),
            day=date.fromisoformat(raw["day"]) if raw["day"] else None,
            week=(int(raw["week"][0]), int(raw["week"][1])) if raw["week"] else None,
            booked_by_hand=tuple(raw.get("booked_by_hand", ())),
            last_decided=(
                datetime.fromisoformat(raw["last_decided"]) if raw.get("last_decided") else None
            ),
            sold_unpriced=tuple(
                SoldHold(
                    position_id=str(hold["position_id"]),
                    contract_id=str(hold["contract_id"]),
                    order_id=str(hold["order_id"]),
                )
                for hold in raw.get("sold_unpriced", ())
            ),
        )

    def save(self, state: LedgerState) -> None:
        data = asdict(state)
        data["day"] = state.day.isoformat() if state.day else None
        data["last_decided"] = state.last_decided.isoformat() if state.last_decided else None
        text = json.dumps(data, default=str, sort_keys=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class Ledger:
    def __init__(
        self,
        risk: RiskProfile,
        instrument: InstrumentProfile,
        store: StateStore,
        *,
        create: bool = False,
        exclusive: bool = False,
    ) -> None:
        """Open the ledger. A missing state file is an error unless `create` is set
        (the very first run): silently starting fresh would clear breakers and a latched
        kill switch that only Andre may clear.
        """
        self._risk = risk
        self._instrument = instrument
        self._store = store
        self._lock_fd: int | None = store.lock() if exclusive else None
        loaded = store.load()
        if loaded is None and not create:
            self.close()
            raise FileNotFoundError("ledger state file is missing; pass create=True on first run")
        self._state = loaded or LedgerState(
            equity=risk.bankroll,
            peak_equity=risk.bankroll,
            realized_today=Decimal(0),
            realized_week=Decimal(0),
            stopouts_today=0,
            trades_today_prime=0,
            trades_today_overnight=0,
            open_position_id=None,
            kill_switch_latched=False,
            day=None,
            week=None,
        )
        if loaded is None:
            store.save(self._state)

    def close(self) -> None:
        """Release the exclusive lock, if this ledger holds it."""
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    @property
    def open_position_id(self) -> str | None:
        return self._state.open_position_id

    @property
    def last_decided(self) -> datetime | None:
        """The newest completed bar a decision has been made on (survives restarts)."""
        return self._state.last_decided

    def mark_decided(self, bar_ts: datetime) -> None:
        """Record, durably, that the bar at `bar_ts` is being decided on. Never moves
        backwards. Raises if it cannot be saved: the caller must not decide then."""
        require_utc(bar_ts, "bar_ts")
        current = self._state.last_decided
        if current is not None and bar_ts <= current:
            return
        self._save(replace(self._state, last_decided=bar_ts))

    @property
    def sold_unpriced(self) -> tuple[SoldHold, ...]:
        """Filled sells with their price pending (D40); survives restarts."""
        return self._state.sold_unpriced

    def hold_sold(self, hold: SoldHold) -> None:
        """Record, durably, a sell the broker reports FILLED with its price pending.
        Raises if it cannot be saved: the caller must stand down then."""
        if hold in self._state.sold_unpriced:
            return
        log.critical(
            "holding %s: %s sold %s, price pending",
            hold.contract_id,
            hold.order_id,
            hold.position_id,
        )
        self._save(replace(self._state, sold_unpriced=(*self._state.sold_unpriced, hold)))

    def release_sold(self, position_id: str) -> None:
        """The sale of `position_id` is booked at its real price: drop its holds."""
        kept = tuple(h for h in self._state.sold_unpriced if h.position_id != position_id)
        if kept != self._state.sold_unpriced:
            self._save(replace(self._state, sold_unpriced=kept))

    def account(self, now: datetime) -> AccountState:
        self._roll(now)
        s = self._state
        return AccountState(
            equity=s.equity,
            peak_equity=s.peak_equity,
            realized_pnl_today=s.realized_today,
            realized_pnl_week=s.realized_week,
            stopouts_today=s.stopouts_today,
            trades_today_prime=s.trades_today_prime,
            trades_today_overnight=s.trades_today_overnight,
            position_open=s.open_position_id is not None,
            kill_switch_latched=s.kill_switch_latched,
        )

    def latch_kill_switch(self, reason: str) -> None:
        if not self._state.kill_switch_latched:
            log.critical("kill switch latched: %s", reason)
            self._save(replace(self._state, kill_switch_latched=True))

    def clear_kill_switch(
        self, cleared_by: str, *, reason: str = "", reset_peak: bool = False
    ) -> None:
        """Andre only. Keeps the drawdown peak unless `reset_peak` is asked for (P6-1)."""
        if not cleared_by.strip():
            raise ValueError("clearing the kill switch needs the name of who cleared it")
        peak = self._state.equity if reset_peak else self._state.peak_equity
        log.critical(
            "kill switch cleared by %s (%s); drawdown peak %s",
            cleared_by,
            reason.strip() or "no reason given",
            f"reset to {peak}" if reset_peak else f"kept at {peak}",
        )
        self._save(replace(self._state, kill_switch_latched=False, peak_equity=peak))

    def book_by_hand(
        self, now: datetime, position_id: str, net_pnl: Decimal, by: str, *, reason: str = ""
    ) -> None:
        """Andre only: book a position the system lost track of, at the net P&L he found
        in the broker's statement. Counts like any other close (stop-outs, limits, drawdown).
        Never clears the kill switch. Operator command: `python -m footprint.live.kill_switch
        book` (G24-4).
        """
        if not by.strip():
            raise ValueError("booking by hand needs the name of who booked it")
        if position_id in self._state.booked_by_hand:
            raise ValueError(f"{position_id!r} was already booked by hand")
        log.critical(
            "%s booked %s by hand at %s (%s)",
            by,
            position_id,
            net_pnl,
            reason.strip() or "no reason given",
        )
        if self._state.open_position_id == position_id:
            self.on_trade_closed(now, position_id, net_pnl)
        else:  # a trade the ledger never recorded (e.g. its write failed at the fill)
            self._book(now, net_pnl, close_position=False)
        self._save(
            replace(
                self._state,
                booked_by_hand=(*self._state.booked_by_hand, position_id),
                sold_unpriced=tuple(
                    h for h in self._state.sold_unpriced if h.position_id != position_id
                ),
            )
        )

    def on_entry_filled(self, now: datetime, position_id: str, session: SessionKind) -> None:
        self._roll(now)
        if self._state.open_position_id is not None:
            raise RuntimeError(
                f"entry {position_id!r} filled while {self._state.open_position_id!r} is open"
            )
        s = self._state
        self._save(
            replace(
                s,
                open_position_id=position_id,
                trades_today_prime=s.trades_today_prime + (session is SessionKind.PRIME),
                trades_today_overnight=s.trades_today_overnight
                + (session is SessionKind.OVERNIGHT),
            )
        )

    def on_trade_closed(self, now: datetime, position_id: str, net_pnl: Decimal) -> None:
        if self._state.open_position_id != position_id:
            raise RuntimeError(
                f"closing {position_id!r} but the open position is {self._state.open_position_id!r}"
            )
        self._book(now, net_pnl, close_position=True)

    def _book(self, now: datetime, net_pnl: Decimal, *, close_position: bool) -> None:
        """Book a close. Closing the open position also releases its D40 holds in the
        same save (the sale is booked, at its real price)."""
        self._roll(now)
        s = self._state
        equity = s.equity + net_pnl
        state = replace(
            s,
            equity=equity,
            peak_equity=max(s.peak_equity, equity),
            realized_today=s.realized_today + net_pnl,
            realized_week=s.realized_week + net_pnl,
            stopouts_today=s.stopouts_today + (net_pnl < 0),
            open_position_id=None if close_position else s.open_position_id,
            sold_unpriced=(
                tuple(h for h in s.sold_unpriced if h.position_id != s.open_position_id)
                if close_position
                else s.sold_unpriced
            ),
        )
        self._save(state)
        if drawdown_breached(self._risk, self.account(now)):
            self.latch_kill_switch(
                f"equity {equity} is down the limit from peak {state.peak_equity}"
            )

    def _roll(self, now: datetime) -> None:
        require_utc(now, "now")
        prime = self._instrument.prime_session
        local = now.astimezone(ZoneInfo(prime.timezone))
        day = local.date() if local.time() >= prime.start else local.date() - timedelta(days=1)
        iso = day.isocalendar()
        week = (iso.year, iso.week)
        s = self._state
        if s.day == day and s.week == week:
            return
        state = s
        if s.day != day:
            state = replace(
                state,
                day=day,
                realized_today=Decimal(0),
                stopouts_today=0,
                trades_today_prime=0,
                trades_today_overnight=0,
            )
        if s.week != week:
            state = replace(state, week=week, realized_week=Decimal(0))
        self._save(state)

    def _save(self, state: LedgerState) -> None:
        self._store.save(state)
        self._state = state
