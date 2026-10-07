"""The mutation runner's own verdicts (independent audit A-9): only a test FAILURE kills
a mutant; a crash, a collection error or "no tests" is an error, never a kill; a group
that fails before any mutant is applied stops the run; every run names its commit."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mutate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "mutate", REPO_ROOT / "qa" / "mutation" / "mutate.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("code", "verdict"),
    [(0, "SURVIVED"), (1, "KILLED"), (2, "ERROR"), (3, "ERROR"), (4, "ERROR"), (5, "ERROR")],
)
def test_only_a_test_failure_kills_a_mutant(code: int, verdict: str) -> None:
    assert _mutate().verdict(code) == verdict


def test_the_tests_run_through_the_projects_own_python() -> None:
    command = _mutate().pytest_command(["tests/x.py"])
    assert command[:5] == ["uv", "run", "python", "-m", "pytest"]
    assert command[-1] == "tests/x.py"


def _spec(tmp_path: Path) -> dict[str, Any]:
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n")
    return {
        "groups": {"g": ["tests/x.py"]},
        "mutants": [["g", str(target), "VALUE = 1", "VALUE = 2", "why"]],
    }


def test_a_group_that_fails_unmutated_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mutate = _mutate()
    monkeypatch.setattr(mutate, "ROOT", Path("/"))
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "1 failed", "")

    monkeypatch.setattr(mutate.subprocess, "run", fake_run)
    monkeypatch.setattr(mutate, "source_line", lambda: "source: abc tree-dirty: 0 files")
    with pytest.raises(SystemExit):
        mutate.run(_spec(tmp_path))
    out = capsys.readouterr().out
    assert "BASELINE" in out
    assert (tmp_path / "target.py").read_text() == "VALUE = 1\n"  # never mutated
    assert len(calls) == 1  # the baseline only


def test_every_run_starts_with_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mutate = _mutate()
    monkeypatch.setattr(mutate, "ROOT", Path("/"))
    codes = iter([0, 1])  # baseline passes; the mutant fails a test

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, next(codes), "", "")

    monkeypatch.setattr(mutate.subprocess, "run", fake_run)
    monkeypatch.setattr(mutate, "source_line", lambda: "source: abc tree-dirty: 0 files")
    survivors = mutate.run(_spec(tmp_path))
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "source: abc tree-dirty: 0 files"
    assert survivors == 0
    assert any("KILLED" in line for line in out)


def test_the_source_line_names_the_commit_and_the_dirty_files() -> None:
    line = _mutate().source_line()
    assert line.startswith("source: ")
    assert "tree-dirty: " in line
