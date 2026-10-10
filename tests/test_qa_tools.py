"""The mutation runner's own verdicts (independent audit A-9): only a test FAILURE kills
a mutant; a crash, a collection error or "no tests" is an error, never a kill; a group
that fails before any mutant is applied stops the run; every run names its commit."""

import importlib.util
import os
import subprocess
import sys
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


def _value_seen(module_dir: Path, env: dict[str, str]) -> str:
    return subprocess.run(
        [sys.executable, "-c", "import m; print(m.VALUE)"],
        cwd=module_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_no_run_reads_bytecode_an_earlier_run_wrote(tmp_path: Path) -> None:
    """G31-1: Python trusts a .pyc whose recorded source mtime (whole seconds) and size
    match. A same-length mutant restored inside the same second left the mutant's
    bytecode in place, so later runs tested code that was not in the file."""
    mutate = _mutate()
    module = tmp_path / "m.py"
    module.write_text("VALUE = 1\n")
    stamp = module.stat()
    base = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    with mutate.fresh_bytecode(base) as env:
        assert _value_seen(tmp_path, env) == "1"
    with mutate.fresh_bytecode(base) as env:  # compile as an unprotected run would
        subprocess.run(
            [sys.executable, "-c", "import py_compile; py_compile.compile('m.py')"],
            cwd=tmp_path,
            env=base,
            check=True,
        )
        module.write_text("VALUE = 2\n")  # same length...
        os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # ...same second
        assert _value_seen(tmp_path, base) == "1"  # the trap is real: stale bytecode
        assert _value_seen(tmp_path, env) == "2"  # the runner's env never reads it


def test_every_test_run_gets_its_own_empty_bytecode_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutate = _mutate()
    monkeypatch.setattr(mutate, "ROOT", Path("/"))
    for inherited in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"):
        monkeypatch.delenv(inherited, raising=False)  # set when this runs under the runner
    envs: list[dict[str, str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert list(Path(env["PYTHONPYCACHEPREFIX"]).iterdir()) == []
        envs.append(env)
        return subprocess.CompletedProcess(command, 0 if len(envs) == 1 else 1, "", "")

    monkeypatch.setattr(mutate.subprocess, "run", fake_run)
    monkeypatch.setattr(mutate, "source_line", lambda: "source: abc tree-dirty: 0 files")
    assert mutate.run(_spec(tmp_path)) == 0
    assert len(envs) == 2  # the baseline and the mutant
    assert envs[0]["PYTHONPYCACHEPREFIX"] != envs[1]["PYTHONPYCACHEPREFIX"]
    assert envs[1]["FOOTPRINT_REQUIRE_IBAPI"] == "1"
