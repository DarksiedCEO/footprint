import ast
import shutil
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from footprint.core.config import ConfigError
from footprint.research import __main__ as cli
from footprint.research.bars import Day
from footprint.research.engine import DayState, Signal, Trade
from footprint.research.rules import RULES, Registered
from footprint.research.settings import OTHER, ResearchSettings, load_research
from tests.conftest import REPO_ROOT
from tests.research.conftest import NY

SRC = REPO_ROOT / "src" / "footprint"


# --- the trading path never imports the research package ------------------------------


def _imported(node: ast.AST) -> list[str]:
    """The module names one import statement brings in."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return []


def _is_research(name: str) -> bool:
    return name == "footprint.research" or name.startswith("footprint.research.")


def research_importers(root: Path) -> list[str]:
    """One entry per import statement outside root/research/ that reaches the package."""
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] == "research":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            hits = [name for name in _imported(node) if _is_research(name)]
            if hits:
                found.append(f"{rel}: {hits[0]}")
    return found


def test_trading_path_never_imports_research() -> None:
    assert (SRC / "research" / "__init__.py").exists()
    assert research_importers(SRC) == []


def test_import_guard_catches_every_spelling(tmp_path: Path) -> None:
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "x.py").write_text("import footprint.research\n")  # allowed inside
    (tmp_path / "a.py").write_text("import footprint.research\n")
    (tmp_path / "b.py").write_text("from footprint.research.engine import run\n")
    (tmp_path / "c.py").write_text("from footprint import research\n")
    (tmp_path / "d.py").write_text("def f():\n    import footprint.research.stats as s\n")
    (tmp_path / "e.py").write_text("import footprint.researcher\nfrom footprint import core\n")
    assert research_importers(tmp_path) == [
        "a.py: footprint.research",
        "b.py: footprint.research.engine",
        "c.py: footprint.research",
        "d.py: footprint.research.stats",
    ]


# --- settings ----------------------------------------------------------------------------


def test_config_is_read(research: ResearchSettings) -> None:
    assert research.timezone == "America/New_York"
    assert research.roll == time(18, 0)
    assert [n for n, _ in research.sessions] == ["asia", "london", "new_york"]
    assert research.vol_divisor == 100
    assert research.vol_max_age_days == 5
    assert research.bankroll == 25000
    assert research.symbol == "NQ"
    assert (research.judging.time_parts, research.judging.min_positive_parts) == (3, 2)
    assert research.gates.min_trades == 200


def _copy(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    return tmp_path / "config"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("timezone: America/New_York", "timezone: Mars/Base", "timezone"),
        ('roll: "18:00"', 'roll: "18:00:30"', "roll"),
        ('roll: "18:00"', 'roll: "6pm"', "roll"),
        (
            'london: {start: "03:00", end: "08:25"}',
            'london: {start: "02:00", end: "08:25"}',
            "overlap",
        ),
        (
            'london: {start: "03:00", end: "08:25"}',
            'london: {start: "03:00", end: "03:00"}',
            "differ",
        ),
        ('london: {start: "03:00", end: "08:25"}', 'other: {start: "03:00", end: "08:25"}', "name"),
        ("divisor: 100", "divisor: 0", "divisor"),
        ("max_age_days: 5", "max_age_days: 0", "max_age_days"),
        ("days_to_expiry: 1", "days_to_expiry: 0", "days_to_expiry"),
        ("days_per_year: 365", "days_per_year: -365", "days_per_year"),
        ("rate: 0 ", "rate: .nan ", "rate"),
        ("rate: 0 ", "rate: true ", "rate"),
        ("reference_price: 20000", "reference_price: 0", "reference_price"),
        ("round_trip_per_contract: 170", "round_trip_per_contract: -1", "round_trip"),
        ("scale_out: {contracts: 2}", "scale_out: {contracts: 1}", "more than one"),
        ("booker: {contracts: 1}", "booker: {contracts: 0}", "contracts"),
        ("min_positive_parts: 2", "min_positive_parts: 4", "min_positive_parts"),
        ("time_parts: 3", "time_parts: 0", "time_parts"),
        ("rules:                        #", "rules: []\nunused:          #", "rules|unknown"),
        ("sessions:", "sessions: {}\nunused:", "sessions|unknown"),
    ],
)
def test_bad_config_is_refused(tmp_path: Path, old: str, new: str, match: str) -> None:
    config = _copy(tmp_path)
    path = config / "research.yaml"
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))
    with pytest.raises(ConfigError, match=match):
        load_research(config, "nq.yaml")


def test_gates_and_strike_step_match_the_backtest_loader() -> None:
    """Read without importing footprint.backtest, but the same values and checks."""
    from footprint.backtest.settings import load_backtest

    backtest = load_backtest(REPO_ROOT / "config" / "backtest.yaml")
    research = load_research(REPO_ROOT / "config", "nq.yaml")
    assert vars_of(research.gates) == vars_of(backtest.gates)
    assert research.option.strike_step == float(backtest.option_model.strike_step)


def vars_of(obj: object) -> dict[str, object]:
    return {name: getattr(obj, name) for name in obj.__slots__}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("min_trades: 200", "min_trades: 0", "min_trades"),
        ("max_drawdown_pct: 15", "max_drawdown_pct: 100", "max_drawdown_pct"),
        ("min_profit_factor: 1.3", "min_profit_factor: -1", "min_profit_factor"),
        ("strike_step: 25", "strike_step: 0", "strike_step"),
        ("  min_trades: 200\n", "", "missing keys"),
    ],
)
def test_bad_backtest_gates_are_refused(tmp_path: Path, old: str, new: str, match: str) -> None:
    config = _copy(tmp_path)
    path = config / "backtest.yaml"
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))
    with pytest.raises(ConfigError, match=match):
        load_research(config, "nq.yaml")


def test_reference_price_null_turns_scaling_off(tmp_path: Path) -> None:
    config = _copy(tmp_path)
    path = config / "research.yaml"
    path.write_text(path.read_text().replace("reference_price: 20000", "reference_price: null"))
    assert load_research(config, "nq.yaml").option.reference_price is None


def test_values_follow_the_profiles(tmp_path: Path) -> None:
    config = _copy(tmp_path)
    risk = config / "risk.yaml"
    risk.write_text(risk.read_text().replace("trail_keep_pct: 50", "trail_keep_pct: 40"))
    s = load_research(config, "nq.yaml")
    assert s.exits.lock_fraction == pytest.approx(0.4)
    assert s.sessions[1][1].start == time(3, 0)
    assert OTHER not in [n for n, _ in s.sessions]


# --- CLI ------------------------------------------------------------------------------


class _Probe:
    name = "probe"

    def entry(self, day: Day, i: int, state: DayState) -> Signal | None:
        if state.trades or day.clock[i] != time(3, 5):
            return None
        return Signal(1, datetime.combine(day.day, time(3, 10), tzinfo=NY))


def test_cli_prints_the_report_and_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(RULES, "probe", Registered(lambda s: (), lambda s, v: _Probe()))
    rows = []
    for d in (4, 5):
        for m in range(15):
            p = 18000 + 4 * m
            rows.append(f"2024-03-{d:02d}T03:{m:02d}:00-05:00,{p},{p + 2},{p - 1},{p + 1},5")
    bars = tmp_path / "bars.csv"
    bars.write_text("ts,open,high,low,close,volume\n" + "\n".join(rows) + "\n")
    vol = tmp_path / "VXN_History.csv"
    vol.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n03/01/2024,1,1,1,20\n")
    code = cli.main(
        [
            "--bars",
            str(bars),
            "--vol",
            str(vol),
            "--rule",
            "probe",
            "--days-to-expiry",
            "7",
            "--config",
            str(REPO_ROOT / "config"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "trades: 2   skipped (no usable vol): 0" in out
    assert "\nall       n=2 " in out
    assert "\nlondon    n=2 " in out
    assert "\nasia      n=0 " in out
    assert "\nother     n=0 " in out
    assert "verdict: FAIL" in out
    assert "FAIL trades: 2 vs >= 200" in out
    assert "7.0 days to expiry" in out
    assert "exit: booker" in out
    assert "exit fills: flat=2" in out
    assert out.count("UNVERIFIED") >= 9
    assert "cost: 170.0 per contract round trip [UNVERIFIED]" in out
    assert "used as implied vol [UNVERIFIED]" in out


def test_cli_refuses_a_bad_days_to_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(RULES, "probe", Registered(lambda s: (), lambda s, v: _Probe()))
    with pytest.raises(SystemExit):
        cli.main(["--bars", "x", "--vol", "y", "--rule", "probe", "--days-to-expiry", "0"])


def test_cli_refuses_a_variant_for_a_rule_without_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(RULES, "probe", Registered(lambda s: (), lambda s, v: _Probe()))
    with pytest.raises(SystemExit):
        cli.main(["--bars", "x", "--vol", "y", "--rule", "probe", "--variant", "1"])


def test_cli_refuses_an_unknown_rule() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--bars", "x", "--vol", "y", "--rule", "nope"])


def test_report_with_no_trades() -> None:
    s = load_research(REPO_ROOT / "config", "nq.yaml")
    text = cli.report([], 3, s, ["h"])
    assert "trades: 0   skipped (no usable vol): 3" in text
    assert "exit fills: none" in text
    assert date(2024, 1, 1).year == 2024


# --- review round 1 (37-M2, 37-M3, 37-L2, 37-L4, data adjustment) ---------------------------


def _trade(net: float, k: int, marks: tuple[float, ...] | None = None) -> Trade:
    at = datetime(2024, 3, 5, 3, 16, tzinfo=NY) + k * timedelta(days=1)
    return Trade(
        day=at.date(),
        session="london",
        direction=1,
        entry_ts=at,
        exit_ts=at + timedelta(minutes=5),
        contracts=1,
        entry_value=80.0,
        net=net,
        reasons=("lock",),
        marks=marks if marks is not None else (-170.0, net),
    )


def test_the_verdict_reads_trades_in_their_order_not_sorted() -> None:
    """37-M2: thirds, the held-out share and drawdown depend on order. Sorted, these
    trades would put every loss in the first third (1 of 3 positive, not 0 of 3)."""
    s = load_research(REPO_ROOT / "config", "nq.yaml")
    trades = [_trade(net, k) for k, net in enumerate([50.0, -100.0, 50.0, -100.0, 50.0, -100.0])]
    text = cli.report(trades, 0, s, ["h"])
    assert "positive thirds: 0 of 3 vs >= 2" in text
    assert "time parts (by trade order): -50.00, -50.00, -50.00" in text


def test_report_shows_the_marked_drawdown_and_ruin() -> None:
    s = load_research(REPO_ROOT / "config", "nq.yaml")
    trades = [_trade(100.0, 0, (-170.0, -6000.0, 100.0)), _trade(100.0, 1)]
    text = cli.report(trades, 0, s, ["h"])
    # 25,000 -> 19,000 inside the first trade, which still closes +100
    assert "max drawdown marked to the model inside trades: 6,000.00 (24.0%)" in text
    assert "FAIL max drawdown %: 24 vs <= 15 (worse of realized 0 and marked 24)" in text
    ruined = [_trade(-30000.0, 0), _trade(500.0, 1)]
    text = cli.report(ruined, 0, s, ["h"])
    assert "RUIN: equity reached zero or below at trade 1 of 2" in text
    assert "FAIL not ruined: ruined at trade 1" in text


def test_report_states_the_known_model_limits() -> None:
    s = load_research(REPO_ROOT / "config", "nq.yaml")
    text = cli.report([], 0, s, ["h"])
    assert "bars must be UNADJUSTED" in text
    assert "stop and lock are judged on the model mid" in text
