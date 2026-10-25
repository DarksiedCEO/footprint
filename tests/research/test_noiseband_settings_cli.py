"""noiseband-es-v1 plumbing: per-instrument research values (es.yaml), the t-statistic
bar, the --cost override and what the report prints."""

import shutil
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.config import ConfigError, load_instrument
from footprint.research import __main__ as cli
from footprint.research.engine import Trade
from footprint.research.settings import load_research
from footprint.research.stats import judge, summarize
from tests.conftest import REPO_ROOT
from tests.research.conftest import NY
from tests.research.test_stats import GATES, _passing

CONFIG = REPO_ROOT / "config"


def _copy(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG, tmp_path / "config")
    return tmp_path / "config"


# --- the ES profile and per-instrument values ------------------------------------------


def test_es_profile_values() -> None:
    es = load_instrument(CONFIG / "instruments" / "es.yaml")
    nq = load_instrument(CONFIG / "instruments" / "nq.yaml")
    assert es.symbol == "ES"
    assert es.exchange == "CME"
    assert es.point_value == Decimal(50)
    assert es.tick_size == Decimal("0.25")
    # everything else mirrors nq.yaml's schema (unused by research, UNVERIFIED for ES)
    assert es.cash_session == nq.cash_session
    assert es.holidays == nq.holidays


def test_research_reads_the_es_values() -> None:
    s = load_research(CONFIG, "es.yaml")
    assert s.symbol == "ES"
    assert s.option.point_value == 50
    assert s.option.reference_price == 7700
    assert s.option.strike_step == 5
    assert s.round_trip_per_contract == 45
    assert (s.option.moneyness_points, s.option.days_to_expiry) == (0, 1)


def test_research_nq_values_are_unchanged() -> None:
    s = load_research(CONFIG, "nq.yaml")
    assert s.option.reference_price == 20000
    assert s.round_trip_per_contract == 170
    assert s.option.strike_step == 25  # null: backtest.yaml's strike step


def test_an_instrument_without_research_values_is_refused(tmp_path: Path) -> None:
    config = _copy(tmp_path)
    shutil.copy(config / "instruments" / "es.yaml", config / "instruments" / "other.yaml")
    with pytest.raises(ConfigError, match=r"other\.yaml"):
        load_research(config, "other.yaml")


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("reference_price: 7700", "reference_price: 0", "reference_price"),
        ("round_trip_per_contract: 45", "round_trip_per_contract: -1", "round_trip"),
        ("strike_step: 5 ", "strike_step: 0 ", "strike_step"),
        ("strike_step: 5 ", "strike_step: x ", "strike_step"),
        ("    strike_step: 5 ", "    other: 1\n    strike_step: 5 ", "unknown"),
        ("noise_band: 2.3", "noise_band: .nan", "min_t_stat"),
        ("noise_band: 2.3", "noise_band: true", "min_t_stat"),
        ("noise_band: 2.3", "noise_band: x", "min_t_stat"),
        ("stop_range_fraction: 0.25", "stop_range_fraction: 0", "stop_range_fraction"),
        ("target_range_fraction: 0.5", "target_range_fraction: -1", "target_range_fraction"),
        ("    contracts: 1              # PRE", "    contracts: 0              # PRE", "contracts"),
    ],
)
def test_bad_es_research_values_are_refused(tmp_path: Path, old: str, new: str, match: str) -> None:
    config = _copy(tmp_path)
    path = config / "research.yaml"
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))
    with pytest.raises(ConfigError, match=match):
        load_research(config, "es.yaml")


def test_t_bars_are_per_rule() -> None:
    s = load_research(CONFIG, "es.yaml")
    assert dict(s.judging.min_t_stat) == {"noise_band": 2.3}


# --- the t bar in the verdict -----------------------------------------------------------


def test_judge_adds_the_t_bar_only_when_given() -> None:
    pnls = _passing()
    t = summarize(pnls, 25000.0).t_stat
    assert t is not None
    plain = judge(pnls, 25000.0, GATES, time_parts=3, min_positive_parts=2)
    assert "t statistic" not in [n for n, _, _ in plain.checks]
    at = judge(pnls, 25000.0, GATES, time_parts=3, min_positive_parts=2, min_t_stat=t)
    assert at.passed  # inclusive
    assert at.checks[-1] == ("t statistic", f"{t:.4g} vs >= {t}", True)
    above = judge(pnls, 25000.0, GATES, time_parts=3, min_positive_parts=2, min_t_stat=t + 1e-9)
    assert not above.passed
    assert [n for n, _, ok in above.checks if not ok] == ["t statistic"]


def test_judge_t_bar_fails_without_a_t_statistic() -> None:
    v = judge([100.0], 25000.0, GATES, time_parts=3, min_positive_parts=0, min_t_stat=-5.0)
    assert ("t statistic", "n/a vs >= -5.0", False) in v.checks


# --- CLI --------------------------------------------------------------------------------


def _rth_csv(tmp_path: Path) -> Path:
    """16 weekdays of regular-session bars closing at 5000 (high and low 1 away: a range
    of 2), with a jump to 5100 at 10:14 on the last day: one call."""
    rows = []
    d = date(2024, 4, 1)
    n = 0
    while n < 16:
        if d.weekday() < 5:
            for m in range(390):
                ts = datetime.combine(d, time(9, 30), tzinfo=NY) + timedelta(minutes=m)
                c = 5100.0 if n == 15 and m >= 44 else 5000.0
                o = 5000.0 if n == 15 and m == 44 else c
                rows.append(f"{ts.isoformat()},{o},{max(o, c) + 1},{min(o, c) - 1},{c},3")
            n += 1
        d += timedelta(days=1)
    path = tmp_path / "es.csv"
    path.write_text("ts,open,high,low,close,volume\n" + "\n".join(rows) + "\n")
    return path


def _vix(tmp_path: Path) -> Path:
    path = tmp_path / "VIX_History.csv"
    path.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n03/28/2024,1,1,1,15\n04/19/2024,1,1,1,16\n")
    return path


def _main(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--bars",
        str(_rth_csv(tmp_path)),
        "--vol",
        str(_vix(tmp_path)),
        "--rule",
        "noise_band",
        "--instrument",
        "es.yaml",
        "--config",
        str(CONFIG),
        *extra,
    ]


def test_cli_runs_the_noise_band_on_es(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Flat history has sigma 0, so the first checkpoint above the open is a call."""
    assert cli.main(_main(tmp_path, "--exit", "adr_bracket")) == 0
    out = capsys.readouterr().out
    assert "trades: 1   skipped (no usable vol): 0" in out
    assert "exit: adr_bracket" in out
    assert "point value 50.0, reference price 7700.0" in out
    assert "strike step 5.0" in out
    assert "cost: 45.0 per contract round trip [UNVERIFIED]" in out
    assert "adr_bracket stop 0.25 and target 0.5 of the rule's range" in out
    assert "FAIL t statistic: n/a vs >= 2.3" in out
    assert "rule notes:" in out
    assert "kept: 16" in out


def test_cli_cost_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(_main(tmp_path, "--cost", "90")) == 0
    out = capsys.readouterr().out
    assert "cost: 90.0 per contract round trip (--cost; config 45.0) [UNVERIFIED]" in out
    assert cli.main(_main(tmp_path, "--cost", "0")) == 0
    assert "cost: 0.0 per contract" in capsys.readouterr().out
    assert cli.main(_main(tmp_path, "--cost", "45")) == 0  # given, even when equal
    assert "cost: 45.0 per contract round trip (--cost; config 45.0)" in capsys.readouterr().out
    assert cli.main(_main(tmp_path)) == 0
    assert "cost: 45.0 per contract round trip [UNVERIFIED]" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["-1", "nan", "inf"])
def test_cli_refuses_a_bad_cost(tmp_path: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        cli.main(_main(tmp_path, "--cost", bad))


def test_cli_cost_changes_the_dollars(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(_main(tmp_path, "--exit", "adr_bracket", "--cost", "30"))
    low = capsys.readouterr().out
    cli.main(_main(tmp_path, "--exit", "adr_bracket", "--cost", "90"))
    high = capsys.readouterr().out
    a = float(low.split("\nall ")[1].split("net=")[1].split()[0].replace(",", ""))
    b = float(high.split("\nall ")[1].split("net=")[1].split()[0].replace(",", ""))
    assert a - b == pytest.approx(60.0)


def test_london_report_has_no_t_bar() -> None:
    s = load_research(CONFIG, "nq.yaml")
    at = datetime(2024, 3, 5, 3, 16, tzinfo=NY)
    trades = [Trade(at.date(), "london", 1, at, at, 1, 80.0, 100.0, ("lock",), (-170.0, 100.0))]
    text = cli.report(trades, 0, s, ["h"])
    assert "t statistic" not in text
    text = cli.report(trades, 0, s, ["h"], min_t_stat=2.3)
    assert "FAIL t statistic: n/a vs >= 2.3" in text
