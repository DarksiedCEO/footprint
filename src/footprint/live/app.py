"""The live system on IBKR's paper account: every part wired together, and the clock.

    build(...)  -> Live: IBKR connection, order adapter, market data, the runner,
                   the ledger and journal on disk, and the alarm line
    Driver      -> the one-second clock: market data first, then the runner; the
                   IBKR connection runs on its own thread

Safety, in order of what can go wrong:
- Paper only, hard-coded (brokers/ibkr/paper.py): a live account or a live port is
  refused here as well as when the settings are made and by the binding. Going live
  is not built or verified, and no setting can switch it on.
- The runner starts reconciling and sends no entry until a person clears it to trade
  by name (`clear <name>` at the prompt). This program never clears it.
- The ledger file must already exist unless `new_ledger` is asked for, so a restart can
  never silently reset the drawdown peak, the breakers or a latched kill switch.
- Every held contract keeps its price stream: the market data reads what the broker
  holds or has orders on, what the order record has sent, and the runner's position,
  every step.
- A failing market data step leaves bars and chains missing (nothing is decided on
  them) and is alarmed; it never stops the runner, which manages what is open.
- The play caller's regime reader reads market data's whole history; a configured
  history too short for it on any day of the calendar refuses the start
  (PlayHistoryTooShort, PC-2) before anything is built or connected.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker, IbkrSettings
from footprint.brokers.ibkr.binding import IbBinding, IbConnectionSettings
from footprint.brokers.ibkr.market import MarketData
from footprint.brokers.ibkr.paper import check_paper
from footprint.core.calendar import calendar_warning
from footprint.core.config import Profiles
from footprint.decision.pipeline import Pipeline
from footprint.decision.play import check_live_history
from footprint.decision.session import NewsBlackout
from footprint.decision.stop import StopKeeper
from footprint.execution.channels import (
    AlarmHub,
    ChannelSettings,
    Post,
    TelegramOnlySettings,
    alarm_hub,
    http_post,
)
from footprint.execution.executor.diego import Executor
from footprint.execution.halt import Halt
from footprint.execution.journal.josephine import Journal
from footprint.execution.ledger import Ledger, StateStore
from footprint.execution.management.malik import Alarm, Manager
from footprint.execution.market import QuoteBook
from footprint.execution.orders import TrackedBroker
from footprint.execution.runner import Runner

log = logging.getLogger("footprint.live")

_TICK = timedelta(seconds=1)


def utc_now() -> datetime:
    return datetime.now(UTC)


class AlarmFile:
    """The record of every alarm: one JSON line per alarm in `path`, and the alarm
    printed to the terminal (the phone channels, D20, come on top). A write that fails
    raises, so its alarm line retries it (and never loses it silently)."""

    def __init__(self, path: Path, out: Any = None) -> None:
        self._path = path
        self._out = out if out is not None else sys.stderr
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, alarm: Alarm) -> None:
        record = {"ts": alarm.ts.isoformat(), "message": alarm.message}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")  # closed (flushed) on the way out
        print(f"ALARM {alarm.ts.isoformat()} {alarm.message}", file=self._out, flush=True)


@dataclass
class Live:
    profiles: Profiles
    settings: IbConnectionSettings
    binding: IbBinding
    adapter: IbkrBroker
    tracked: TrackedBroker
    contracts: ContractBook
    book: QuoteBook
    market: MarketData
    runner: Runner
    ledger: Ledger
    halt: Halt
    alarms: AlarmHub
    clock: Callable[[], datetime] = field(default=utc_now)
    _calendar_checked: date | None = field(default=None, init=False)

    def held(self) -> frozenset[str]:
        """Every contract whose prices must keep streaming: what IBKR holds or has
        orders on, what the order record has sent and not seen finish, and the
        runner's open position."""
        ids = set(self.adapter.held_contracts())
        ids |= {order.contract_id for order in self.tracked.unfinished.values()}
        position = self.runner.position
        if position is not None:
            ids.add(position.contract_id)
        return frozenset(ids)

    def alarm(self, message: str) -> None:
        self.alarms.submit(Alarm(self.clock(), message))

    def check_calendar(self) -> None:
        """Alarm once a day while the exchange calendar is about to run out, or has."""
        p = self.profiles.instrument
        today = self.clock().astimezone(ZoneInfo(p.prime_session.timezone)).date()
        if today == self._calendar_checked:
            return
        self._calendar_checked = today
        warning = calendar_warning(today, p)
        if warning is not None:
            self.alarm(warning)


def build(
    profiles: Profiles,
    settings: IbConnectionSettings,
    state_dir: Path,
    sink: Callable[[Alarm], None],
    blackouts: Callable[[datetime], Iterable[NewsBlackout]],
    *,
    channels: ChannelSettings | TelegramOnlySettings | None = None,
    post: Post = http_post,
    new_ledger: bool = False,
    client_factory: Callable[[Any], Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> Live:
    """Everything, wired; nothing connected yet (the Driver starts the connection)."""
    p = profiles
    ibkr = p.execution.ibkr
    check_paper(settings.account, settings.port)  # LiveAccountRefused
    check_live_history(p)  # PlayHistoryTooShort: the regime reader needs this history
    state_dir.mkdir(parents=True, exist_ok=True)
    alarms = alarm_hub(sink, p.execution.alarms, channels, post, clock)  # D20

    def alarm(message: str) -> None:
        alarms.submit(Alarm(clock(), message))

    scope = (p.instrument.symbol, ibkr.option_security_type)
    binding = IbBinding(settings, scope, alarm, client_factory)
    contracts = ContractBook()
    book = QuoteBook()
    runner_box: list[Runner] = []

    def resynced() -> None:  # IBKR resynced: reconcile before anything else
        if runner_box:
            runner_box[0].doubt()

    adapter = IbkrBroker(
        binding, contracts, IbkrSettings.from_config(ibkr), scope, alarm, on_resync=resynced
    )
    binding.attach(adapter)
    tracked = TrackedBroker(adapter)
    halt = Halt()
    ledger = Ledger(
        p.risk,
        p.instrument,
        StateStore(state_dir / "ledger.json"),
        create=new_ledger,
        exclusive=True,
    )
    pipeline = Pipeline(p)
    stops = StopKeeper(p.risk, p.instrument, p.execution)
    executor = Executor(p, pipeline, tracked, stops, ledger, halt, book)
    manager = Manager(p, tracked, stops, ledger, halt, book)
    held_box: list[Live] = []

    def held() -> frozenset[str]:
        return held_box[0].held() if held_box else frozenset()

    market = MarketData(
        binding, contracts, book.publish, p.instrument, p.execution, alarm, clock, held=held
    )
    binding.attach_market(market)
    runner = Runner(
        pipeline,
        executor,
        manager,
        ledger,
        Journal(state_dir / "journal"),
        halt,
        bars=market.bars,
        chain=market.chain,
        blackouts=blackouts,
        on_alarm=alarms.submit,
        started=clock(),
        book=book,
        settings=p.execution.runner,
    )
    runner_box.append(runner)
    live = Live(
        p,
        settings,
        binding,
        adapter,
        tracked,
        contracts,
        book,
        market,
        runner,
        ledger,
        halt,
        alarms,
        clock,
    )
    held_box.append(live)
    live.check_calendar()
    return live


class Driver:
    """The clock: one step a second (market data, then the runner), and the operator's
    commands between steps. The IBKR connection runs on its own thread."""

    def __init__(
        self,
        live: Live,
        out: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._live = live
        self._out = out if out is not None else sys.stdout
        self._sleep = sleep
        self._market_failing = False
        self._attended_at = live.clock()  # started at the terminal by a person
        self._attendance_warned = False

    def tick(self, now: datetime) -> None:
        """One second: fresh quotes and bars first, then the decisions on them."""
        try:
            self._live.market.step(now)
            if self._market_failing:
                self._market_failing = False
                self._live.alarm("market data is stepping again")
        except Exception as exc:  # no data, no decisions; the runner still manages
            log.exception("market data step failed")
            if not self._market_failing:
                self._market_failing = True
                self._live.alarm(f"market data step failed ({exc!r}): no new data until fixed")
        self._live.runner.step(now)
        self._live.check_calendar()
        self._check_attendance(now)

    def _check_attendance(self, now: datetime) -> None:
        """DEGRADED_ALERTING blocks unattended trading: with no input at the terminal
        for `degraded_attendance_minutes`, entries stop (open positions are still
        managed) until someone clears it again."""
        live = self._live
        if not live.alarms.mode.degraded or live.runner.cleared_by is None:
            return
        window = timedelta(minutes=live.profiles.execution.alarms.degraded_attendance_minutes)
        idle = now - self._attended_at
        if idle > window:
            live.runner.revoke_clearance(
                f"UNATTENDED while {live.alarms.mode.value}: nothing typed at the terminal "
                f"for {idle / timedelta(minutes=1):.0f} min. Entries stopped (anything open is "
                "still managed). Type 'clear <name>' to resume"
            )
        elif idle > window * 0.8 and not self._attendance_warned:
            self._attendance_warned = True
            left = (window - idle) / timedelta(minutes=1)
            self._say(
                f"DEGRADED_ALERTING: type 'here' within {left:.0f} min or entries stop "
                "(unattended trading is blocked without full alarms)"
            )

    def command(self, line: str, stop: threading.Event) -> None:
        """`clear <name>` (clear to trade), `here` (still attended), `status`, `quit`.
        Any command counts as attended."""
        word, _, rest = line.strip().partition(" ")
        if word:
            self._attended_at, self._attendance_warned = self._live.clock(), False
        if word == "here":
            self._say(f"noted: attended. alerting: {self._live.alarms.mode.value}")
        elif word == "clear":
            name = rest.strip()
            if not name:
                self._say("clear needs the name of who clears it: clear <name>")
                return
            self._live.runner.clear_to_trade(name)
            self._say(f"cleared to trade by {name}")
        elif word == "status":
            self._say(self.status())
        elif word == "quit":
            held = self._live.held()
            if held:
                self._say(
                    f"WARNING: stopping with {sorted(held)} held or working. Nothing manages "
                    "it now: only the resting stop protects it, and only until IBKR's session "
                    "ends (DAY). The next start flattens any leftover. Flatten by hand or "
                    "check the stop in IBKR now"
                )
            self._say("stopping")
            stop.set()
        elif word:
            self._say(f"unknown command {word!r}: clear <name> | here | status | quit")

    def status(self) -> str:
        live = self._live
        now = live.clock()
        bars = live.market.bars(now)
        chain = live.market.chain(now)
        position = live.runner.position
        lines = [
            f"alerting: {live.alarms.mode.value}",
            *(f"  {h}" for h in live.alarms.health),
            f"cleared to trade: {live.runner.cleared_by or 'no'}",
            f"time {now.isoformat()}",
            f"IBKR ready: {live.adapter.ready}; refused: {live.binding.refused or 'no'}",
            f"front future: {live.market.front.expiry if live.market.front else 'not yet'}",
            f"bars: {len(bars)}; newest {bars[-1].ts.isoformat() if bars else 'none'}",
            f"chain: {len(chain.quotes) if chain else 'none'} quotes",
            f"reconciling: {live.runner.reconciling}; halt: {list(live.halt.reasons) or 'no'}",
            f"position: {position.contract_id if position else 'none'}",
            f"alarms waiting: {live.alarms.pending}",
        ]
        return "\n".join(lines)

    def run(self, stop: threading.Event, commands: queue.Queue[str] | None = None) -> None:
        """Until `stop` is set: the connection on its own thread, a step every second."""
        live = self._live
        worker = threading.Thread(target=live.binding.run, args=(stop,), name="ibkr", daemon=True)
        worker.start()
        self._say(
            f"PAPER account {live.settings.account} at {live.settings.host}:{live.settings.port}; "
            "reconciling first; no entries until 'clear <name>'"
        )
        if live.alarms.mode.degraded:
            self._say(
                f"*** {live.alarms.mode.value} ***\n"
                "This is NOT full alarm coverage. Unattended trading is blocked: entries stop "
                f"after {live.profiles.execution.alarms.degraded_attendance_minutes} min without "
                "input here ('here' keeps it attended)"
            )
        try:
            while not stop.is_set():
                started = live.clock()
                self.tick(started)
                while commands is not None and not commands.empty():
                    self.command(commands.get_nowait(), stop)
                if live.binding.refused is not None:
                    self._say(f"IBKR refused the connection: {live.binding.refused}")
                    stop.set()
                    break
                spent = (live.clock() - started).total_seconds()
                self._sleep(max(0.0, _TICK.total_seconds() - spent))
        finally:
            stop.set()
            live.binding.stop()
            ibkr = live.profiles.execution.ibkr
            worker.join(timeout=float(ibkr.ack_timeout_seconds + ibkr.reconnect_seconds))
            live.alarms.close(timedelta(seconds=live.profiles.execution.alarms.retry_seconds))

    def _say(self, message: str) -> None:
        print(message, file=self._out, flush=True)
