"""A small mutation runner: each mutant changes one exact piece of source; the named
tests must then FAIL (the mutant is killed). A mutant that survives is a behaviour no
test pins down.

    uv run python qa/mutation/mutate.py qa/mutation/remediation.json

Spec: {"groups": {name: [test paths]}, "mutants": [[group, file, old, new, why], ...]}.
`old` must occur exactly once in `file`. The source is restored after every mutant,
and on SIGINT/SIGTERM. A mutant whose tests run past the time limit counts as killed
(TIMEOUT: the tests did not pass) and is reported as such.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType

ROOT = Path(__file__).resolve().parents[2]
LIMIT_SECONDS = 600
_ACTIVE: list[tuple[Path, str]] = []


def _restore(signum: int, frame: FrameType | None) -> None:
    for path, original in _ACTIVE:
        path.write_text(original)
    print(f"INTERRUPTED (signal {signum}): source restored", flush=True)
    sys.exit(2)


def run(spec: dict[str, object]) -> int:
    groups = spec["groups"]
    mutants = spec["mutants"]
    if not isinstance(groups, dict) or not isinstance(mutants, list):
        raise SystemExit("spec needs a 'groups' mapping and a 'mutants' list")
    survivors = 0
    env = {**os.environ, "FOOTPRINT_REQUIRE_IBAPI": "1"}
    for number, (group, file, old, new, why) in enumerate(mutants, start=1):
        path = ROOT / file
        original = path.read_text()
        if original.count(old) != 1:
            print(f"BAD MUTANT {number} ({original.count(old)} matches): {file}: {old!r}")
            survivors += 1
            continue
        _ACTIVE.append((path, original))
        path.write_text(original.replace(old, new))
        try:
            result = subprocess.run(
                ["uv", "run", "pytest", "-q", "-x", "-p", "no:cacheprovider", *groups[group]],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
                timeout=LIMIT_SECONDS,
                check=False,
            )
            verdict = "KILLED  " if result.returncode != 0 else "SURVIVED"
        except subprocess.TimeoutExpired:
            verdict = "KILLED  (TIMEOUT)"
        finally:
            path.write_text(original)
            _ACTIVE.clear()
        survivors += verdict.startswith("SURVIVED")
        print(f"{number:3} {verdict} [{group}] {file}: {why}", flush=True)
    print(f"{len(mutants)} mutants, {survivors} survived", flush=True)
    return survivors


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _restore)
    signal.signal(signal.SIGINT, _restore)
    sys.exit(1 if run(json.loads(Path(sys.argv[1]).read_text())) else 0)
