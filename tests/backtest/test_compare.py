"""Q6 (ii): the comparison runner runs every entry rule against every exit rule through
the single-run command, and summarises by copying each run's readout.json.

Synthetic bars here prove the plumbing only: nothing about any rule's profitability.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from footprint.backtest import __main__ as single_module
from footprint.backtest import compare, rules
from footprint.backtest.__main__ import main as single_main
from footprint.backtest.compare import NO_PROFIT_CLAIM, main
from footprint.backtest.settings import RuleSettings
from footprint.core.config import Profiles
from footprint.execution.management.malik import ExitRule
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config"
_POSITION_SUFFIX = re.compile(rb'("position_id": "[0-9T]+-\d+-)[0-9a-f]{8}"')
_TWO_BY_TWO = {"entry": ("breakout", "stairstep"), "exit": ("target", "trail")}


def _cut_to_two_by_two(patch: pytest.MonkeyPatch) -> None:
    """The registries cut down to two entry and two exit rules (restored on undo)."""
    for name in [n for n in rules.ENTRY_RULES if n not in _TWO_BY_TWO["entry"]]:
        patch.delitem(rules.ENTRY_RULES, name)
    for name in [n for n in rules.EXIT_RULES if n not in _TWO_BY_TWO["exit"]]:
        patch.delitem(rules.EXIT_RULES, name)


@pytest.fixture(scope="module")
def bars(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bars") / "bars.csv"
    write_bars(path)
    return path


@pytest.fixture
def two_by_two() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        _cut_to_two_by_two(patch)
        yield


def _compare(bars: Path, out: Path, *extra: str) -> tuple[int, str, str]:
    printed, errors = io.StringIO(), io.StringIO()
    argv = ["--bars", str(bars), "--out", str(out), "--config", str(CONFIG)]
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(errors):
        code = main([*argv, "--trade-from", TRADE_FROM.isoformat(), *extra])
    return code, printed.getvalue(), errors.getvalue()


@pytest.fixture(scope="module")
def compared(bars: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """One 2x2 comparison shared by the tests that only read it."""
    out = tmp_path_factory.mktemp("shared") / "cmp"
    with pytest.MonkeyPatch.context() as patch:
        _cut_to_two_by_two(patch)
        code, printed, _ = _compare(bars, out)
    assert code == 0
    return out, printed


def _copy(compared: tuple[Path, str], tmp_path: Path) -> Path:
    out = tmp_path / "cmp"
    shutil.copytree(compared[0], out)
    return out


class _Recorder:
    """A stand-in for the single run: records its argv, writes nothing, returns 0."""

    def __init__(self, seen: list[list[str]]) -> None:
        self.seen = seen

    def __call__(self, argv: list[str]) -> int:
        self.seen.append(argv)
        return 0


def _csv_rows(out: Path) -> list[list[str]]:
    with (out / "summary.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_two_by_two_runs_every_pair_in_sorted_order(compared: tuple[Path, str]) -> None:
    out, printed = compared
    expected = [("breakout", "target"), ("breakout", "trail"), ("stairstep", "target"),
                ("stairstep", "trail")]  # fmt: skip
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [f"{e}__{x}" for e, x in expected] + ["summary.csv", "summary.md"]
    )
    for e, x in expected:
        for name in ("readout.md", "readout.json", "ledger.json"):
            assert (out / f"{e}__{x}" / name).is_file()
    assert [(r[0], r[1]) for r in _csv_rows(out)[1:]] == expected
    assert printed == (out / "summary.md").read_text(encoding="utf-8")


def test_pairs_are_every_entry_against_every_exit_sorted(two_by_two: None) -> None:
    assert compare.pairs() == [
        ("breakout", "target"),
        ("breakout", "trail"),
        ("stairstep", "target"),
        ("stairstep", "trail"),
    ]


def test_summary_values_are_copied_from_each_runs_readout_json(
    compared: tuple[Path, str],
) -> None:
    out = compared[0]
    header, *rows = _csv_rows(out)
    markdown = (out / "summary.md").read_text(encoding="utf-8")
    assert len(rows) == len(_TWO_BY_TWO["entry"]) * len(_TWO_BY_TWO["exit"])
    for row in rows:
        readout = json.loads((out / f"{row[0]}__{row[1]}" / "readout.json").read_text())
        expected = [row[0], row[1], readout["verdict"]]
        names = ["entry", "exit", "verdict"]
        for g in readout["gates"]:
            names += [f"{g['segment']} {g['gate']}", f"{g['segment']} {g['gate']} result"]
            expected += [g["actual"], "pass" if g["passed"] else "FAIL"]
        assert header == names
        assert row == expected
        assert "| " + " | ".join(expected) + " |" in markdown
    assert "| " + " | ".join(header) + " |" in markdown


def test_the_gates_cover_trades_and_both_segments(compared: tuple[Path, str]) -> None:
    header = _csv_rows(compared[0])[0]
    for segment in ("all", "out of sample"):
        for gate in ("trades", "win rate %", "payoff ratio", "profit factor", "max drawdown %"):
            assert f"{segment} {gate}" in header
            assert f"{segment} {gate} result" in header


def test_the_summary_heads_with_the_data_config_and_no_profit_claim(
    compared: tuple[Path, str], bars: Path
) -> None:
    out = compared[0]
    run = json.loads((out / "breakout__target" / "readout.json").read_text())["run"]
    head = (out / "summary.md").read_text(encoding="utf-8").split("| entry |")[0]
    assert NO_PROFIT_CLAIM == "Synthetic or unverified data says nothing about profitability."
    assert NO_PROFIT_CLAIM in head
    assert f"- bars: {bars}\n" in head
    assert f"- first bar: {run['first_bar']}\n" in head
    assert f"- last bar: {run['last_bar']}\n" in head
    assert f"- config: {CONFIG}\n" in head
    assert f"- trade from: {TRADE_FROM.isoformat()}\n" in head


def test_without_trade_from_the_summary_says_the_first_bar(
    two_by_two: None, compared: tuple[Path, str], bars: Path
) -> None:
    markdown, _ = compare.summarise(compared[0], bars, CONFIG)
    assert "- trade from: the first bar\n" in markdown


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _normalised(data: bytes, root: Path) -> bytes:
    return _POSITION_SUFFIX.sub(rb'\1<RAND>"', data.replace(str(root).encode(), b"<RUN>"))


def test_a_pair_run_alone_equals_the_same_pair_inside_compare(
    compared: tuple[Path, str], bars: Path, tmp_path: Path
) -> None:
    inside = compared[0] / "stairstep__target"
    alone = tmp_path / "alone"
    with contextlib.redirect_stdout(io.StringIO()):
        single_main(
            ["--bars", str(bars), "--out", str(alone), "--config", str(CONFIG),
             "--trade-from", TRADE_FROM.isoformat(),
             "--entry", "stairstep", "--exit", "target"]
        )  # fmt: skip
    files = _files(alone)
    assert files == _files(inside)
    assert "readout.json" in files
    for name in files:
        assert _normalised((alone / name).read_bytes(), alone) == _normalised(
            (inside / name).read_bytes(), inside
        ), name


def test_the_real_registry_runs_all_twelve_pairs(bars: Path, tmp_path: Path) -> None:
    out = tmp_path / "cmp"
    code, _, _ = _compare(bars, out)
    assert code == 0
    expected = [(e, x) for e in sorted(rules.ENTRY_RULES) for x in sorted(rules.EXIT_RULES)]
    assert len(expected) == len(rules.ENTRY_RULES) * len(rules.EXIT_RULES)
    assert compare.pairs() == expected
    assert [(r[0], r[1]) for r in _csv_rows(out)[1:]] == expected
    for e, x in expected:
        assumptions = json.loads((out / f"{e}__{x}" / "readout.json").read_text())["assumptions"]
        assert assumptions["entry rule"] == e
        assert assumptions["exit rule"] == x


def test_options_reach_every_run(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackouts = tmp_path / "news.csv"
    blackouts.write_text("name,start,end\n", encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(single_module, "main", _Recorder(seen))
    out = tmp_path / "cmp"
    code, _, _ = _compare(bars, out, "--blackouts", str(blackouts), "--instrument", "other.yaml")
    assert code == 1  # the stand-in writes no readout: the first pair fails, by design
    assert seen == [
        ["--bars", str(bars), "--out", str(out / "breakout__target"), "--config", str(CONFIG),
         "--instrument", "other.yaml", "--blackouts", str(blackouts),
         "--trade-from", TRADE_FROM.isoformat(), "--entry", "breakout", "--exit", "target"]
    ]  # fmt: skip


def test_without_options_the_single_run_gets_its_defaults(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(single_module, "main", _Recorder(seen))
    out = tmp_path / "cmp"
    with contextlib.redirect_stderr(io.StringIO()):
        assert main(["--bars", str(bars), "--out", str(out)]) == 1
    assert seen == [
        ["--bars", str(bars), "--out", str(out / "breakout__target"), "--config", "config",
         "--instrument", "nq.yaml", "--entry", "breakout", "--exit", "target"]
    ]  # fmt: skip


def test_a_bad_trade_from_is_refused_before_anything_runs(
    two_by_two: None, bars: Path, tmp_path: Path
) -> None:
    out = tmp_path / "cmp"
    with pytest.raises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
        main(["--bars", str(bars), "--out", str(out), "--trade-from", "not-a-time"])
    assert refused.value.code == 2
    assert not out.exists()


def test_a_non_empty_out_is_refused_and_nothing_runs(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "cmp"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    called: list[list[str]] = []
    monkeypatch.setattr(single_module, "main", _Recorder(called))
    code, printed, errors = _compare(bars, out)
    assert code == 1
    assert called == []
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]
    assert "refused" in errors
    assert printed == ""


def test_an_out_that_is_a_file_is_refused(two_by_two: None, bars: Path, tmp_path: Path) -> None:
    out = tmp_path / "cmp"
    out.write_text("a file", encoding="utf-8")
    code, _, errors = _compare(bars, out)
    assert code == 1
    assert out.read_text(encoding="utf-8") == "a file"
    assert "refused" in errors


def test_an_existing_empty_out_is_used(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = single_module.main
    ran: list[str] = []

    def counted(argv: list[str]) -> int:
        ran.append(argv[argv.index("--out") + 1])
        return real(argv)

    monkeypatch.setattr(single_module, "main", counted)
    out = tmp_path / "cmp"
    out.mkdir()
    assert _compare(bars, out)[0] == 0
    assert len(ran) == len(compare.pairs())
    assert (out / "summary.md").is_file()


def _broken(profiles: Profiles, rule_settings: RuleSettings) -> ExitRule | None:
    raise RuntimeError("deliberately broken")


def test_one_failed_run_stops_everything_names_the_pair_and_writes_no_summary(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(rules.EXIT_RULES, "trail", _broken)  # breakout__trail is 2nd
    out = tmp_path / "cmp"
    code, printed, errors = _compare(bars, out)
    assert code == 1
    assert "breakout__trail" in errors
    assert "deliberately broken" in errors
    assert (out / "breakout__target" / "readout.json").is_file()  # completed run kept
    assert not (out / "stairstep__target").exists()  # nothing after the failure ran
    assert not list(out.glob("summary*"))
    assert printed == ""


def test_a_run_that_exits_is_a_failure_too(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exits(argv: list[str]) -> int:
        raise SystemExit(2)

    monkeypatch.setattr(single_module, "main", exits)
    code, _, errors = _compare(bars, tmp_path / "cmp")
    assert code == 1
    assert "breakout__target failed: exit 2" in errors  # the run's own exit code, named
    assert not list((tmp_path / "cmp").glob("summary*"))


def test_a_run_that_writes_no_readout_is_a_failure(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(single_module, "main", lambda argv: 0)
    code, _, errors = _compare(bars, tmp_path / "cmp")
    assert code == 1
    assert "breakout__target" in errors
    assert "no readout.json" in errors


def test_runs_over_different_bars_are_not_summarised(
    two_by_two: None, compared: tuple[Path, str], bars: Path, tmp_path: Path
) -> None:
    out = _copy(compared, tmp_path)
    path = out / "stairstep__trail" / "readout.json"
    readout = json.loads(path.read_text())
    readout["run"]["last_bar"] = "1999-01-01T00:00:00+00:00"
    path.write_text(json.dumps(readout), encoding="utf-8")
    with pytest.raises(compare.CompareError, match="stairstep__trail"):
        compare.summarise(out, bars, CONFIG)
    readout["run"]["first_bar"] = "1999-01-01T00:00:00+00:00"
    readout["run"]["last_bar"] = json.loads(
        (out / "breakout__target" / "readout.json").read_text()
    )["run"]["last_bar"]
    path.write_text(json.dumps(readout), encoding="utf-8")
    with pytest.raises(compare.CompareError, match="stairstep__trail"):
        compare.summarise(out, bars, CONFIG)


def test_runs_with_different_gates_are_not_summarised(
    two_by_two: None, compared: tuple[Path, str], bars: Path, tmp_path: Path
) -> None:
    out = _copy(compared, tmp_path)
    path = out / "breakout__trail" / "readout.json"
    readout = json.loads(path.read_text())
    readout["gates"] = readout["gates"][1:]
    path.write_text(json.dumps(readout), encoding="utf-8")
    with pytest.raises(compare.CompareError, match="breakout__trail"):
        compare.summarise(out, bars, CONFIG)


def test_a_failed_summary_through_main_writes_nothing(
    two_by_two: None, bars: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> tuple[str, str]:
        raise compare.CompareError("run x__y has different gates; not summarised")

    monkeypatch.setattr(compare, "summarise", refuse)
    out = tmp_path / "cmp"
    code, printed, errors = _compare(bars, out)
    assert code == 1
    assert "different gates" in errors
    assert not list(out.glob("summary*"))
    assert printed == ""


def test_rows_are_never_reordered_by_result(
    two_by_two: None, compared: tuple[Path, str], bars: Path, tmp_path: Path
) -> None:
    out = _copy(compared, tmp_path)
    # Make the last pair the "best" by every number; its row must stay last.
    path = out / "stairstep__trail" / "readout.json"
    readout = json.loads(path.read_text())
    readout["verdict"] = "PASS"
    for g in readout["gates"]:
        g["passed"] = True
    path.write_text(json.dumps(readout), encoding="utf-8")
    markdown, table = compare.summarise(out, bars, CONFIG)
    order = [line.split(",")[:2] for line in table.splitlines()[1:]]
    assert order == [list(p) for p in compare.pairs()]
    assert markdown.rstrip().splitlines()[-1].startswith("| stairstep | trail | PASS |")
