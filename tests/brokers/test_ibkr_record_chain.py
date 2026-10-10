"""Q3 slice 3: `python -m footprint.brokers.ibkr.record_chain` itself (needs ibapi:
skipped here, required in CI). The loop is tested without ibapi in
test_chain_recorder_loop.py."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

if not os.environ.get("FOOTPRINT_REQUIRE_IBAPI"):
    pytest.importorskip("ibapi", reason="ibapi not installed: scripts/install_ibapi.py")

from footprint.brokers.ibkr import record_chain as record_module
from footprint.brokers.ibkr.binding import IbBinding, IbConnectionSettings
from footprint.brokers.ibkr.check import check
from footprint.brokers.ibkr.record_chain import main
from footprint.core.config import Profiles
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
    monkeypatch.setattr(record_module, "read_env_file", lambda path: {})
    monkeypatch.setattr(IbConnectionSettings, "from_env", lambda env, ibkr: settings())


def args(out: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--out",
        str(out),
        "--config",
        str(ROOT / "config"),
        "--env",
        str(tmp_path / ".env"),
        *extra,
    ]


def test_main_runs_the_read_only_check_with_the_recorder_as_its_market_action(
    tmp_path: Path,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_check(profiles: Profiles, *rest: Any, **kw: Any) -> int:
        seen.append(kw)
        return 0

    clock = Clock()
    out = tmp_path / "data" / "chain"
    assert main(args(out, tmp_path, "--minutes", "3"), fake_check, clock, clock.sleep) == 0
    assert callable(seen[0]["market_action"])
    assert seen[0]["clock"] is clock
    assert out.is_dir()  # created, parents too


def test_minutes_below_one_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(args(tmp_path, tmp_path, "--minutes", "0"), lambda *a, **k: 0)
    assert exit_.value.code == 2


def test_an_out_that_is_a_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "chain"
    out.write_text("keep me", encoding="utf-8")
    assert main(args(out, tmp_path), lambda *a, **k: 0) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert out.read_text(encoding="utf-8") == "keep me"


def test_without_quotes_the_recorder_writes_nothing_and_sends_no_order(
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
    out = tmp_path / "chain"
    code = main(args(out, tmp_path, "--wait", "2", "--minutes", "2"), run_check, clock, clock.sleep)
    text = capsys.readouterr().out
    assert code == 1
    assert "resynced" in text
    assert "no fresh chain" in text
    assert "0 snapshots written" in text
    assert list(out.iterdir()) == []
    sent = [name for g in gateways for name in g.sent]
    assert "reqContractDetails" in sent  # MarketData looked the future up
    assert ORDER_CALLS.isdisjoint(sent)
    assert all(not g.connected for g in gateways)
