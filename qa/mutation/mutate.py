"""A small mutation runner: each mutant changes one exact piece of source; the named
tests must then FAIL (the mutant is killed). A mutant that survives is a behaviour no
test pins down.

    uv run python qa/mutation/mutate.py qa/mutation/remediation.json

Spec: {"groups": {name: [test paths]}, "mutants": [[group, file, old, new, why], ...]}.
`old` must occur exactly once in `file`. The source is restored after every mutant,
and on SIGINT/SIGTERM. A mutant whose tests run past the time limit counts as killed
(TIMEOUT: the tests did not pass) and is reported as such.

Evidence rules (independent audit A-9):
- The first line names the source: its commit and how many files under src/ and
  tests/ differ from it. Evidence without it is not bound to a commit.
- Each group is run once UNMUTATED first; a group that does not pass stops the run
  (BASELINE FAILED), so a broken environment can never be read as kills.
- Only a test FAILURE (pytest exit 1) kills a mutant. A collection or import error, an
  interrupted run, "no tests collected" or any other exit is ERROR, never a kill, and
  counts against the run like a survivor.
- Tests run as `uv run python -m pytest`, so a copied tree tests its own source (the
  `pytest` launcher of a copy points at the original's environment).
- Every test run gets its own new, empty bytecode cache and writes none (G31-1).
  Python trusts a .pyc whose recorded source mtime (whole seconds) and size match, so a
  same-length mutant restored inside the same second used to leave its bytecode
  behind: later mutants, baselines and gates in that tree then ran code that was not
  in the file (a false KILL was seen).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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


def verdict(returncode: int) -> str:
    """pytest's exit code: 1 = tests failed (killed), 0 = all passed (survived)."""
    if returncode == 1:
        return "KILLED"
    if returncode == 0:
        return "SURVIVED"
    return "ERROR"


def pytest_command(tests: list[str]) -> list[str]:
    return ["uv", "run", "python", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests]


def source_line() -> str:
    """The commit the evidence is about, and how far the tree differs from it."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    return f"source: {sha or 'UNKNOWN'} tree-dirty: {len(dirty)} files"


@contextmanager
def fresh_bytecode(base: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """`base` plus a new, empty bytecode cache that is never written to (G31-1): Python
    reads .pyc files only from PYTHONPYCACHEPREFIX, so no run can see bytecode another
    run, or the tree's own __pycache__, holds."""
    with tempfile.TemporaryDirectory(prefix="mutate-pyc-") as cache:
        yield {**base, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": cache}


def _tests(group: list[str], env: dict[str, str]) -> int:
    with fresh_bytecode(env) as isolated:
        return subprocess.run(
            pytest_command(group),
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=isolated,
            timeout=LIMIT_SECONDS,
            check=False,
        ).returncode


def run(spec: dict[str, object]) -> int:
    groups = spec["groups"]
    mutants = spec["mutants"]
    if not isinstance(groups, dict) or not isinstance(mutants, list):
        raise SystemExit("spec needs a 'groups' mapping and a 'mutants' list")
    print(source_line(), flush=True)
    survivors = 0
    env = {**os.environ, "FOOTPRINT_REQUIRE_IBAPI": "1"}
    for name in sorted({m[0] for m in mutants}):
        code = _tests(groups[name], env)
        if code != 0:
            print(f"BASELINE FAILED [{name}]: exit {code} with no mutant applied", flush=True)
            raise SystemExit(2)
        print(f"baseline [{name}]: passed", flush=True)
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
            outcome = verdict(_tests(groups[group], env))
        except subprocess.TimeoutExpired:
            outcome = "KILLED (TIMEOUT)"
        finally:
            path.write_text(original)
            _ACTIVE.clear()
        survivors += not outcome.startswith("KILLED")
        print(f"{number:3} {outcome:8} [{group}] {file}: {why}", flush=True)
    print(f"{len(mutants)} mutants, {survivors} survived or errored", flush=True)
    return survivors


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _restore)
    signal.signal(signal.SIGINT, _restore)
    sys.exit(1 if run(json.loads(Path(sys.argv[1]).read_text())) else 0)
