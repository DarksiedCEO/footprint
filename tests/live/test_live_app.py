"""The live start-up program: everything wired once, paper only, never cleared to trade
by itself, and a held contract's prices always streamed."""

from __future__ import annotations

import io
import json
import queue
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from footprint.core.config import Profiles, load_profiles
from footprint.execution.management.malik import Alarm
from footprint.execution.orders import Order, OrderType, Side
from footprint.live.app import AlarmFile, Driver, Live, build
from tests.brokers.test_ibkr_binding import ACCOUNT, Recording, settings
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)


@pytest.fixture
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


class Sink:
    def __init__(self) -> None:
        self.got: list[Alarm] = []

    def __call__(self, alarm: Alarm) -> None:
        self.got.append(alarm)


def make(profiles: Profiles, tmp_path: Path, **kw: Any) -> tuple[Live, Sink]:
    sink = Sink()
    live = build(
        profiles,
        settings(),
        tmp_path / "state",
        sink,
        lambda now: (),
        new_ledger=kw.pop("new_ledger", True),
        client_factory=kw.pop("client_factory", Recording),
        clock=kw.pop("clock", lambda: NOW),
        **kw,
    )
    return live, sink


def delivered(live: Live, sink: Sink) -> list[str]:
    live.alarms.close(timedelta(seconds=2))
    return [a.message for a in sink.got]


# --- build ---------------------------------------------------------------------------------


def test_a_live_config_is_refused(profiles: Profiles, tmp_path: Path) -> None:
    live_config = replace(
        profiles,
        execution=replace(
            profiles.execution, ibkr=replace(profiles.execution.ibkr, require_paper=False)
        ),
    )
    with pytest.raises(ValueError, match="paper account only"):
        make(live_config, tmp_path)


def test_settings_that_do_not_require_paper_are_refused(profiles: Profiles, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="paper account only"):
        build(profiles, settings(require_paper=False), tmp_path, Sink(), lambda now: ())


def test_a_missing_ledger_is_never_created_unless_asked(profiles: Profiles, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        make(profiles, tmp_path, new_ledger=False)
    live, _ = make(profiles, tmp_path, new_ledger=True)
    assert (tmp_path / "state" / "ledger.json").exists()
    live.alarms.close(timedelta(seconds=1))
    again, _ = make(profiles, tmp_path, new_ledger=False)  # a restart opens the same file
    again.alarms.close(timedelta(seconds=1))


def test_everything_is_wired_to_one_another(profiles: Profiles, tmp_path: Path) -> None:
    live, sink = make(profiles, tmp_path)
    assert live.binding._events is live.adapter
    assert live.binding._market is live.market
    assert live.runner._bars == live.market.bars
    assert live.runner._chain == live.market.chain
    assert live.market._publish == live.book.publish
    assert live.runner._book is live.book
    messages = delivered(live, sink)
    assert any("STARTED: reconciling" in m for m in messages)
    assert any("STARTED WITHOUT HOLIDAYS" in m for m in messages)  # nq.yaml has none yet


def test_an_ibkr_resync_sends_the_runner_to_reconcile(profiles: Profiles, tmp_path: Path) -> None:
    live, _ = make(profiles, tmp_path)
    live.runner._reconciling = False
    live.adapter._on_resync()
    assert live.runner.reconciling
    live.alarms.close(timedelta(seconds=1))


def test_held_names_the_broker_the_order_record_and_the_position(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, _ = make(profiles, tmp_path)
    monkeypatch.setattr(live.adapter, "held_contracts", lambda: frozenset({"conid:9"}))
    live.tracked._sent["p1-entry"] = Order(
        "p1-entry", "NQ 20261007 C20000 [1]", Side.BUY, 1, OrderType.LIMIT, Decimal(1)
    )

    class Held:
        contract_id = "NQ 20261007 P19900 [2]"

    live.runner._position = Held()  # type: ignore[assignment]
    assert live.held() == {"conid:9", "NQ 20261007 C20000 [1]", "NQ 20261007 P19900 [2]"}
    assert live.market._held() == live.held()  # the market data reads exactly this
    live.alarms.close(timedelta(seconds=1))


# --- the clock ------------------------------------------------------------------------


def test_a_failing_market_step_is_alarmed_once_and_the_runner_still_steps(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, sink = make(profiles, tmp_path)
    steps: list[datetime] = []
    monkeypatch.setattr(live.runner, "step", steps.append)

    def broken(now: datetime) -> None:
        raise RuntimeError("market data bug")

    monkeypatch.setattr(live.market, "step", broken)
    driver = Driver(live, out=io.StringIO())
    driver.tick(NOW)
    driver.tick(NOW + timedelta(seconds=1))
    monkeypatch.setattr(live.market, "step", lambda now: None)
    driver.tick(NOW + timedelta(seconds=2))
    assert steps == [NOW + timedelta(seconds=k) for k in range(3)]
    messages = delivered(live, sink)
    assert sum("market data step failed" in m for m in messages) == 1
    assert sum("stepping again" in m for m in messages) == 1


def test_market_data_steps_before_the_runner(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, _ = make(profiles, tmp_path)
    order: list[str] = []
    monkeypatch.setattr(live.market, "step", lambda now: order.append("market"))
    monkeypatch.setattr(live.runner, "step", lambda now: order.append("runner"))
    Driver(live, out=io.StringIO()).tick(NOW)
    assert order == ["market", "runner"]
    live.alarms.close(timedelta(seconds=1))


def test_clearing_to_trade_needs_a_name_and_is_never_automatic(
    profiles: Profiles, tmp_path: Path
) -> None:
    live, sink = make(profiles, tmp_path)
    out = io.StringIO()
    driver = Driver(live, out=out)
    stop = threading.Event()
    assert live.runner._cleared_by is None  # built: not cleared
    driver.command("clear", stop)
    assert live.runner._cleared_by is None
    assert "needs the name" in out.getvalue()
    driver.command("clear Andre\n", stop)
    assert live.runner._cleared_by == "Andre"
    assert any("cleared to trade by Andre" in m for m in delivered(live, sink))


def test_status_quit_and_unknown_commands(profiles: Profiles, tmp_path: Path) -> None:
    live, _ = make(profiles, tmp_path)
    out = io.StringIO()
    driver = Driver(live, out=out)
    stop = threading.Event()
    driver.command("status", stop)
    text = out.getvalue()
    for part in ("IBKR ready: False", "bars: 0", "chain: none", "reconciling: True"):
        assert part in text
    driver.command("sell everything", stop)
    assert "unknown command 'sell'" in out.getvalue()
    assert not stop.is_set()
    driver.command("quit", stop)
    assert stop.is_set()
    live.alarms.close(timedelta(seconds=1))


def test_the_driver_runs_the_connection_steps_and_shuts_down_cleanly(
    profiles: Profiles, tmp_path: Path
) -> None:
    clients: list[Recording] = []

    def factory(wrapper: Any) -> Recording:
        client = Recording(wrapper)

        def session(c: Recording) -> None:
            wrapper.managedAccounts(ACCOUNT)
            wrapper.nextValidId(100)
            while c.connected:
                threading.Event().wait(0.01)

        client.on_run = session
        clients.append(client)
        return client

    live, _ = make(profiles, tmp_path, client_factory=factory)
    stop = threading.Event()
    commands: queue.Queue[str] = queue.Queue()
    ticks: list[float] = []

    def sleep(seconds: float) -> None:
        ticks.append(seconds)
        threading.Event().wait(0.02)
        if len(ticks) == 30:
            commands.put("quit")

    Driver(live, out=io.StringIO(), sleep=sleep).run(stop, commands)
    assert stop.is_set()
    assert clients, "never connected"
    calls = [name for c in clients for name, _ in c.calls]
    assert "reqMarketDataType" in calls  # real-time data asked for at the session open
    assert "reqContractDetails" in calls  # the market data looked up the futures
    assert not clients[-1].connected  # hung up on the way out
    assert all(0 <= t <= 1 for t in ticks)


def test_a_refused_account_stops_the_driver(profiles: Profiles, tmp_path: Path) -> None:
    def factory(wrapper: Any) -> Recording:
        client = Recording(wrapper)
        client.on_run = lambda c: wrapper.managedAccounts("DU0000000")
        return client

    live, _ = make(profiles, tmp_path, client_factory=factory)
    out = io.StringIO()
    stop = threading.Event()

    def nap(seconds: float) -> None:
        threading.Event().wait(0.01)

    Driver(live, out=out, sleep=nap).run(stop)
    assert "IBKR refused the connection" in out.getvalue()


# --- the alarm file and the news file -------------------------------------------------


def test_the_alarm_file_keeps_every_alarm_and_prints_it(tmp_path: Path) -> None:
    out = io.StringIO()
    sink = AlarmFile(tmp_path / "logs" / "alarms.log", out)
    sink(Alarm(NOW, "first"))
    sink(Alarm(NOW, "second"))
    lines = (tmp_path / "logs" / "alarms.log").read_text().splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["first", "second"]
    assert "ALARM 2026-10-06T14:00:00+00:00 first" in out.getvalue()


def test_news_windows_are_read_in_utc_and_need_an_offset(tmp_path: Path) -> None:
    from footprint.data.news import blackouts_from

    path = tmp_path / "news.csv"
    path.write_text("name,start,end\nCPI,2026-10-14T08:28:00-04:00,2026-10-14T08:32:00-04:00\n")
    (window,) = blackouts_from(path)(NOW)
    assert window.start == datetime(2026, 10, 14, 12, 28, tzinfo=UTC)
    assert window.start.tzinfo is UTC
    assert list(blackouts_from(None)(NOW)) == []
    path.write_text("name,start,end\nCPI,2026-10-14T08:28:00,2026-10-14T08:32:00\n")
    with pytest.raises(ValueError, match="no UTC offset"):
        blackouts_from(path)


def test_the_program_needs_a_news_choice(capsys: pytest.CaptureFixture[str]) -> None:
    from footprint.live.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["--env", "nowhere"])
    assert exc.value.code == 2
    assert "--blackouts" in capsys.readouterr().err
