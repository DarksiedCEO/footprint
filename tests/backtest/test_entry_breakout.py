"""Q5.1 entry "breakout": the EXISTING momentum entry, exposed under a name, unchanged.

Andre (2026-10-10): "the EXISTING momentum entry, unchanged ... Expose it under the
name; change no logic." So `--entry breakout` must decide exactly as the default run:
the same trigger (Darius, the momentum reader, built from the same strategy.volume
settings), the same decisions, the same trades, the same files. The only difference
a named run is allowed is the readout's assumption line `entry rule: breakout`.
"""

from __future__ import annotations

import json
from pathlib import Path

from footprint.agents.momentum_reader import MomentumReader
from footprint.backtest.rules import ENTRY_RULES
from footprint.backtest.settings import load_backtest
from footprint.core.config import Profiles
from footprint.core.models import Score
from footprint.decision.pipeline import Pipeline
from tests.backtest.test_rules_seam import _normalised, _run
from tests.conftest import REPO_ROOT
from tests.decision.plays import open_play
from tests.decision.scenario import DECISION_TIME, chain_for, market
from tests.decision.test_console_pipeline import clean

RULES = load_backtest(REPO_ROOT / "config" / "backtest.yaml").rules


def test_breakout_is_registered_and_builds_the_momentum_reader_from_the_live_settings(
    profiles: Profiles,
) -> None:
    trigger = ENTRY_RULES["breakout"](profiles, RULES)
    assert type(trigger) is MomentumReader
    live = MomentumReader(profiles.strategy.volume, profiles.instrument)
    assert vars(trigger) == vars(live)
    assert trigger._settings is profiles.strategy.volume


def test_breakout_reads_the_scenario_exactly_as_the_live_reader(
    profiles: Profiles,
) -> None:
    trigger = ENTRY_RULES["breakout"](profiles, RULES)
    live = MomentumReader(profiles.strategy.volume, profiles.instrument)
    bars = market()
    seen = set()
    # The last ten prefixes (each read sees the whole history before it; one tuple per
    # prefix, shared by both readers, as the pipeline shares it); the scenario's
    # decision bar is the last one.
    for end in range(len(bars) - 9, len(bars) + 1):
        prefix = tuple(bars[:end])
        got, want = trigger.read(prefix), live.read(prefix)
        assert got == want, end
        seen.add(got.score)
    assert Score.NEUTRAL in seen  # the scenario has bars where it does not fire
    assert Score.BULLISH in seen  # and the bar where it does


def test_breakout_reaches_the_same_decision_as_the_default_pipeline(profiles: Profiles) -> None:
    bars = market()
    chain = chain_for(bars[-1].close)
    default = Pipeline(profiles, play=open_play()).decide(
        bars, chain, clean(profiles), (), DECISION_TIME
    )
    named = Pipeline(profiles, ENTRY_RULES["breakout"](profiles, RULES), play=open_play()).decide(
        bars, chain, clean(profiles), (), DECISION_TIME
    )
    assert default.fired
    assert named.fired
    assert named.verdict == default.verdict
    assert named.plan == default.plan
    assert named.entry == default.entry
    assert named.reasons() == default.reasons()


def _without_entry_rule_line(text: str) -> str:
    kept = [line for line in text.splitlines(keepends=True) if "entry rule" not in line]
    assert len(kept) == len(text.splitlines(keepends=True)) - 1, "exactly one line differs"
    return "".join(kept)


def test_a_breakout_run_writes_exactly_the_default_run_plus_the_rule_name(
    tmp_path: Path,
) -> None:
    default_dir, named_dir = tmp_path / "default", tmp_path / "named"
    default_dir.mkdir()
    named_dir.mkdir()
    default_out, default_printed = _run(default_dir)
    named_out, named_printed = _run(named_dir, "--entry", "breakout")

    def files(out: Path) -> list[str]:
        return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())

    assert files(named_out) == files(default_out)
    # The scenario trades, so the entry and the exit are both compared.
    assert (default_out / "journal" / "trades.jsonl").read_text().strip()
    for name in files(default_out):
        want = _normalised((default_out / name).read_bytes(), default_dir)
        got = _normalised((named_out / name).read_bytes(), named_dir)
        if name == "readout.json":
            want_json, got_json = json.loads(want), json.loads(got)
            assert got_json["assumptions"].pop("entry rule") == "breakout"
            assert "entry rule" not in want_json["assumptions"]
            assert got_json == want_json
        elif name == "readout.md":
            assert "- entry rule: breakout" in got.decode()
            assert _without_entry_rule_line(got.decode()) == want.decode()
        else:
            assert got == want, name
    want_printed = _normalised(default_printed.encode(), default_dir).decode()
    got_printed = _normalised(named_printed.encode(), named_dir).decode()
    assert "entry rule" not in want_printed
    assert _without_entry_rule_line(got_printed) == want_printed
