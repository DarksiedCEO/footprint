"""Write the speed work's reference hashes (BH-11) from the code as it is now.

    uv run python qa/speed/make_reference.py [--long] [--out DIR] [NAME ...]

Run ONLY on the code before a speed change (the reference must come from the old
code): tests/backtest/test_speed_equivalence.py then proves a speed change leaves
every output byte the same. Writes tests/backtest/golden/speed/<scenario>.json with
the commit it was made on (or DIR/<scenario>.json with --out, to compare without
overwriting). It applies the backtest tests' stand-ins, as tests/backtest/conftest.py
does inside pytest: the always-TREND play caller (unless the scenario runs the real
one) and no scheduled news windows (`no_scheduled_news`: the engine's with_news_rules
and the CLI readout's news_assumptions).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.backtest.conftest import STAND_IN  # noqa: E402
from tests.backtest.speed_runs import FAST, LONG, REFERENCE, Scenario, run_hashes  # noqa: E402
from tests.decision.plays import open_play  # noqa: E402

from footprint.backtest import __main__ as cli  # noqa: E402
from footprint.backtest import engine  # noqa: E402
from footprint.decision.play import PlayCaller  # noqa: E402


def main(argv: list[str]) -> int:
    scenarios = (*FAST, *LONG) if "--long" in argv else FAST
    out = REFERENCE
    if "--out" in argv:
        at = argv.index("--out")
        out = Path(argv[at + 1])
        argv = argv[:at] + argv[at + 2 :]
    names = [a for a in argv if not a.startswith("--")]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    if dirty:
        print("refused: src/ differs from HEAD; the reference must come from committed code")
        return 2
    real = engine.PlayCaller
    real_rules, real_lines = engine.with_news_rules, cli.news_assumptions
    # conftest's no_scheduled_news, outside pytest: the CSV windows alone, readout OFF.
    engine.with_news_rules = lambda profiles, extra: extra  # type: ignore[assignment,return-value]
    cli.news_assumptions = lambda profiles, source: {"news rules": f"OFF ({STAND_IN})"}
    try:
        _run(scenarios, names, commit, out, real)
    finally:
        engine.PlayCaller = real
        engine.with_news_rules = real_rules
        cli.news_assumptions = real_lines
    return 0


def _run(
    scenarios: tuple[Scenario, ...],
    names: list[str],
    commit: str,
    out: Path,
    real: type[PlayCaller],
) -> None:
    for scenario in scenarios:
        if names and scenario.name not in names:
            continue
        # The backtest tests' stand-in (tests/backtest/conftest.py), outside pytest.
        engine.PlayCaller = real if scenario.real_play else (lambda *a, **k: open_play())  # type: ignore[assignment,misc]
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            hashes, summary = run_hashes(scenario, Path(tmp))
        seconds = time.monotonic() - started
        out.mkdir(parents=True, exist_ok=True)
        record = {
            "scenario": scenario.name,
            "made_on": commit,
            "hashes": hashes,
            "summary": summary,
        }
        path = out / f"{scenario.name}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{scenario.name}: {summary}, {seconds:.0f} s -> {path}", flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
