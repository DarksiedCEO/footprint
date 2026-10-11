"""Q2: `python -m footprint.brokers.ibkr.history` itself (needs ibapi: skipped here,
required in CI). The export logic is tested without ibapi in test_history_export.py."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

if not os.environ.get("FOOTPRINT_REQUIRE_IBAPI"):
    pytest.importorskip("ibapi", reason="ibapi not installed: scripts/install_ibapi.py")

from footprint.brokers.ibkr import history as history_module
from footprint.brokers.ibkr.binding import IbBinding, IbConnectionSettings
from footprint.brokers.ibkr.check import check
from footprint.brokers.ibkr.history import main
from footprint.core.config import Profiles, load_profiles
from footprint.data.csv_export import partial_path
from tests.brokers.test_ibkr_binding import settings
from tests.brokers.test_ibkr_check import SimulatedGateway

ROOT = Path(__file__).resolve().parents[2]
ORDER_CALLS = {"placeOrder", "cancelOrder", "reqGlobalCancel"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def paper_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # no .env is read: the paper settings the other binding tests use
    monkeypatch.setattr(history_module, "read_env_file", lambda path: {})
    monkeypatch.setattr(IbConnectionSettings, "from_env", lambda env, ibkr: settings())


def args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--out",
        str(tmp_path / "nq_1m.csv"),
        "--config",
        str(ROOT / "config"),
        "--env",
        str(tmp_path / ".env"),
        *extra,
    ]


def test_main_runs_the_read_only_check_with_the_export_as_its_market_action(
    tmp_path: Path,
) -> None:
    seen: list[tuple[Profiles, dict[str, Any]]] = []

    def fake_check(profiles: Profiles, *rest: Any, **kw: Any) -> int:
        seen.append((profiles, kw))
        return 0

    clock = Clock()
    assert main(args(tmp_path, "--days", "3"), fake_check, clock, clock.sleep) == 0
    profiles, kw = seen[0]
    assert profiles.execution.market_data.history_days == 3
    assert callable(kw["market_action"])
    assert kw["clock"] is clock
    configured = load_profiles(ROOT / "config", "nq.yaml").execution.market_data.history_days
    seen.clear()
    main(args(tmp_path), fake_check, clock, clock.sleep)
    assert seen[0][0].execution.market_data.history_days == configured


def test_days_below_one_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(args(tmp_path, "--days", "0"), lambda *a, **k: 0)
    assert exit_.value.code == 2


def test_without_history_the_export_writes_nothing_and_sends_no_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateways: list[SimulatedGateway] = []

    def factory(wrapper: Any) -> SimulatedGateway:
        gateways.append(SimulatedGateway(wrapper))
        return gateways[-1]

    def run_check(p: Profiles, s: IbConnectionSettings, wait: float, out: Any, **kw: Any) -> int:
        return check(
            p, s, wait, out, lambda st, scope, alarm: IbBinding(st, scope, alarm, factory), **kw
        )

    clock = Clock()
    code = main(args(tmp_path, "--wait", "2"), run_check, clock, clock.sleep)
    out = capsys.readouterr().out
    wait = load_profiles(ROOT / "config", "nq.yaml").execution.market_data.export_wait_seconds
    assert code == 1
    assert "resynced" in out
    assert f"NO HISTORY after {wait} s" in out
    assert not (tmp_path / "nq_1m.csv").exists()
    assert not partial_path(tmp_path / "nq_1m.csv").exists()
    sent = [name for g in gateways for name in g.sent]
    assert "reqContractDetails" in sent  # MarketData looked the future up
    assert ORDER_CALLS.isdisjoint(sent)
    assert all(not g.connected for g in gateways)


def test_an_existing_file_is_refused_and_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "nq_1m.csv"
    target.write_text("keep me", encoding="utf-8")

    def run_check(p: Profiles, s: IbConnectionSettings, wait: float, out: Any, **kw: Any) -> int:
        return check(
            p,
            s,
            wait,
            out,
            lambda st, scope, alarm: IbBinding(st, scope, alarm, SimulatedGateway),
            **kw,
        )

    clock = Clock()
    assert main(args(tmp_path, "--wait", "2"), run_check, clock, clock.sleep) == 1
    assert "already exists" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "keep me"
