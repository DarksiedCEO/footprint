"""noiseband-v1 review round 1: M-1 (t bar names, fail closed), L-3 (mid-day-gap days
the keep rule drops), L-4 (vol index tied to the instrument), NB-11 (a gap pushes the
entry bar to the flat time) and NB-12 (a kept day ending before the flat time)."""

import shutil
from datetime import date, datetime, time
from pathlib import Path

import pytest

from footprint.core.config import ConfigError
from footprint.research import __main__ as cli
from footprint.research.engine import run
from footprint.research.rules import RULES, check_t_bars
from footprint.research.rules.noise_band import build_noise
from footprint.research.settings import ResearchSettings, load_research
from footprint.research.vol import VolIndex, vol_matches
from tests.conftest import REPO_ROOT
from tests.research.conftest import NY, make_day
from tests.research.test_noise_band import DATES, HIST, RTH_BARS, _join, prepared, rth, signals
from tests.research.test_noiseband_settings_cli import _main, _rth_csv, _vix

CONFIG = REPO_ROOT / "config"
VOL = VolIndex((date(2024, 1, 1),), (20.0,), divisor=100.0, max_age_days=400)


def _copy(tmp_path: Path) -> Path:
    shutil.copytree(CONFIG, tmp_path / "config")
    return tmp_path / "config"


def _edit(config: Path, old: str, new: str) -> None:
    path = config / "research.yaml"
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))


# --- M-1: t bars -----------------------------------------------------------------------


def test_a_t_bar_for_a_rule_without_settings_is_refused_at_load(tmp_path: Path) -> None:
    config = _copy(tmp_path)
    _edit(config, "    noise_band: 2.3", "    noise_bnad: 2.3")
    with pytest.raises(ConfigError, match="noise_bnad"):
        load_research(config, "es.yaml")


def test_cli_refuses_a_t_bar_for_an_unregistered_rule(tmp_path: Path) -> None:
    config = _copy(tmp_path)
    _edit(config, "    noise_band: 2.3", "    noise_band: 2.3\n    ghost: 2.0")
    _edit(
        config,
        "rules:                        # per-rule settings, read by each rule",
        "rules:                        # per-rule settings, read by each rule\n  ghost: {}",
    )
    load_research(config, "es.yaml")  # a rules section exists: the loader cannot tell
    args = _main(tmp_path)
    args[args.index("--config") + 1] = str(config)
    with pytest.raises(SystemExit):
        cli.main(args)


def test_check_t_bars_refuses_names_not_registered(research: ResearchSettings) -> None:
    from dataclasses import replace

    bad = replace(research, judging=replace(research.judging, min_t_stat={"ghost": 2.0}))
    with pytest.raises(ConfigError, match="ghost"):
        check_t_bars(bad, "london_orb")


def test_a_rule_that_requires_a_t_bar_is_refused_without_one(research: ResearchSettings) -> None:
    from dataclasses import replace

    assert RULES["noise_band"].t_bar_required
    assert not RULES["london_orb"].t_bar_required
    none = replace(research, judging=replace(research.judging, min_t_stat={}))
    with pytest.raises(ConfigError, match="t bar"):
        check_t_bars(none, "noise_band")
    check_t_bars(none, "london_orb")  # London has none, by its pre-registration
    check_t_bars(research, "noise_band")


def test_cli_refuses_a_noise_band_run_without_its_t_bar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy(tmp_path)
    _edit(
        config,
        "    noise_band: 2.3           # PRE-REGISTERED (signed): two scenarios tested\n",
        "",
    )
    _edit(config, "  min_t_stat:                 #", "  min_t_stat: {}              #")
    assert load_research(config, "es.yaml").judging.min_t_stat == {}
    args = _main(tmp_path)
    args[args.index("--config") + 1] = str(config)
    with pytest.raises(SystemExit):
        cli.main(args)
    assert "t bar" in capsys.readouterr().err


def test_cli_prints_the_t_bar_in_use(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(_main(tmp_path)) == 0
    assert "t bar: 2.3 (judging.min_t_stat.noise_band)" in capsys.readouterr().out


# --- L-4: the vol index belongs to the instrument -------------------------------------


def test_vol_index_per_instrument() -> None:
    assert load_research(CONFIG, "es.yaml").vol_index == "VIX"
    assert load_research(CONFIG, "nq.yaml").vol_index == "VXN"


@pytest.mark.parametrize(
    ("name", "index", "ok"),
    [
        ("VIX_History.csv", "VIX", True),
        ("vix_history.csv", "VIX", True),
        ("VIX.csv", "VIX", True),
        ("VIX_History_2026.csv", "VIX", True),
        ("VXN_History.csv", "VIX", False),
        ("VIX9D_History.csv", "VIX", False),
        ("MYVIX_History.csv", "VIX", False),
        ("history.csv", "VIX", False),
        ("VXN_History.csv", "VXN", True),
    ],
)
def test_vol_file_name_must_name_the_index(name: str, index: str, ok: bool) -> None:
    assert vol_matches(Path("/x") / name, index) is ok


def test_cli_refuses_a_vol_file_of_another_index(tmp_path: Path) -> None:
    args = _main(tmp_path)
    wrong = tmp_path / "VXN_History.csv"
    shutil.copy(_vix(tmp_path), wrong)
    args[args.index("--vol") + 1] = str(wrong)
    with pytest.raises(SystemExit):
        cli.main(args)


def test_cli_prints_the_vol_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(_main(tmp_path)) == 0
    assert "vol index VIX (file VIX_History.csv)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    vol_index: VIX", '    vol_index: ""'),
        ("    vol_index: VIX", "    vol_index: 7"),
        ("    vol_index: VIX", "    vol_index: VI_X"),
    ],
)
def test_bad_vol_index_is_refused(tmp_path: Path, old: str, new: str) -> None:
    config = _copy(tmp_path)
    _edit(config, old, new)
    with pytest.raises(ConfigError, match="vol_index"):
        load_research(config, "es.yaml")


# --- NB-11: the next bar is at or after the flat time ----------------------------------


def _gap_day(next_minute: int) -> object:
    """Regular bars at 100 for minutes 0..373, a call trigger at 374 (close 120), then
    bars from `next_minute` to 389."""
    body = rth(DATES[14], [100.0] * 374, opens=[100.0] * 374)
    trigger = rth(DATES[14], [120.0], opens=[100.0], first_minute=374)
    tail_n = RTH_BARS - next_minute
    tail = rth(DATES[14], [120.0] * tail_n, opens=[120.0] * tail_n, first_minute=next_minute)
    return _join(_join(body, trigger), tail)


def test_a_signal_whose_next_bar_opens_at_the_flat_time_is_refused_and_reported(
    research: ResearchSettings,
) -> None:
    d = _gap_day(385)  # the next bar opens 15:55
    r = prepared(research, [*HIST, d])  # type: ignore[list-item]
    assert signals(r, d) == []  # type: ignore[arg-type]
    lines = r.report([*HIST, d])  # type: ignore[list-item]
    assert f"NB-11 signals refused, next bar at or after the flat time: 1: {DATES[14]}" in lines
    s = research
    trades, _ = run([*HIST, d], build_noise(s, None), VOL, s, "booker")  # type: ignore[list-item]
    assert trades == []


def test_a_signal_whose_next_bar_opens_before_the_flat_time_trades(
    research: ResearchSettings,
) -> None:
    d = _gap_day(384)  # 15:54
    r = prepared(research, [*HIST, d])  # type: ignore[list-item]
    assert signals(r, d) == [(374, 1)]  # type: ignore[arg-type]
    assert "NB-11 signals refused, next bar at or after the flat time: 0" in r.report(
        [*HIST, d]  # type: ignore[list-item]
    )


def test_nb12_report_line(research: ResearchSettings) -> None:
    early = rth(DATES[14], [120.0] * 360, opens=[100.0, *[120.0] * 359])
    r = prepared(research, [*HIST, early])
    assert f"NB-12 kept days ending before the flat time (not traded): 1: {DATES[14]}" in (
        r.report([*HIST, early])
    )
    r = prepared(research, HIST)
    assert "NB-12 kept days ending before the flat time (not traded): 0" in r.report(HIST)


# --- L-3: dropped weekdays with regular bars to the close (mid-day gaps) ---------------


def test_dropped_days_with_a_mid_day_gap_are_listed(research: ResearchSettings) -> None:
    morning = rth(DATES[14], [100.0] * 100)  # minutes 0..99
    afternoon = rth(DATES[14], [100.0] * 199, first_minute=191)  # 191..389: 299 bars in all
    gap = _join(morning, afternoon)
    early_close = rth(DATES[15], [100.0] * 210)  # 09:30-12:59: an announced early close
    one_short = rth(DATES[15], [100.0] * 299, first_minute=90)  # 11:00-15:58: ends 15:58
    saturday = rth(date(2024, 4, 20), [100.0] * 299, first_minute=91)
    r = prepared(research, [*HIST, gap, early_close])
    lines = r.report([*HIST, gap, early_close])
    want = (
        "L-3 dropped weekdays with regular bars to the close (mid-day gaps, lookahead): "
        f"1: {DATES[14]}"
    )
    assert want in lines
    r = prepared(research, [*HIST, one_short, saturday])
    lines = r.report([*HIST, one_short, saturday])
    assert (
        "L-3 dropped weekdays with regular bars to the close (mid-day gaps, lookahead): 0" in lines
    )


def test_a_post_close_bar_does_not_count_as_running_to_the_close(
    research: ResearchSettings,
) -> None:
    early = rth(DATES[14], [100.0] * 210)
    late = make_day(datetime.combine(DATES[14], time(16, 0), tzinfo=NY), [100.0], day=DATES[14])
    d = _join(early, late)
    r = prepared(research, [*HIST, d])
    assert (
        "L-3 dropped weekdays with regular bars to the close (mid-day gaps, lookahead): 0"
        in r.report([*HIST, d])
    )


def test_cli_rule_notes_show_the_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _rth_csv(tmp_path)
    assert cli.main(_main(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "L-3 dropped weekdays" in out
    assert "NB-11 signals refused" in out
    assert "NB-12 kept days ending" in out


def test_settings_lines_say_when_no_t_bar_is_configured() -> None:
    import argparse

    s = load_research(CONFIG, "nq.yaml")
    args = argparse.Namespace(
        rule="london_orb",
        variant="500",
        exit="booker",
        bars="b",
        vol=Path("VXN_History.csv"),
        cost=None,
    )
    lines = cli.settings_lines(s, args, s.round_trip_per_contract)
    assert "t bar: none configured" in lines
    args.rule = "noise_band"
    assert "t bar: 2.3 (judging.min_t_stat.noise_band)" in cli.settings_lines(s, args, 170.0)


def test_nb12_counts_only_days_that_could_trade(research: ResearchSettings) -> None:
    early = rth(DATES[13], [120.0] * 360, opens=[100.0, *[120.0] * 359])  # still warming up
    r = prepared(research, [*HIST[:13], early])
    assert "NB-12 kept days ending before the flat time (not traded): 0" in r.report(
        [*HIST[:13], early]
    )
