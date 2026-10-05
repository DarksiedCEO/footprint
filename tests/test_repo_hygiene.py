import re
import shutil
import subprocess

import pytest
import yaml

from tests.conftest import REPO_ROOT


def test_dotenv_is_listed_in_gitignore() -> None:
    lines = [line.strip() for line in (REPO_ROOT / ".gitignore").read_text().splitlines()]
    assert ".env" in lines
    assert "!.env" not in lines


def test_git_actually_ignores_dotenv_but_not_the_example() -> None:
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not running inside a git checkout")

    def ignored(name: str) -> bool:
        result = subprocess.run([git, "check-ignore", "-q", name], cwd=REPO_ROOT, check=False)
        return result.returncode == 0

    assert ignored(".env")
    assert not ignored(".env.example")
    tracked = subprocess.run(
        [git, "ls-files", ".env"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    )
    assert tracked.stdout.strip() == ""


WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PINNED_USES = re.compile(r"^\s*(?:- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*$")


def test_ci_actions_are_pinned_to_full_commit_shas_with_a_version_comment() -> None:
    uses = [
        line
        for workflow in sorted(WORKFLOWS.glob("*.y*ml"))
        for line in workflow.read_text().splitlines()
        if "uses:" in line
    ]
    assert len(uses) >= 2
    unpinned = [line.strip() for line in uses if not PINNED_USES.match(line)]
    assert not unpinned, f"actions not pinned to a commit SHA with a version comment: {unpinned}"


def test_ci_runs_all_four_checks_on_push_and_pull_request() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    assert {"push", "pull_request"} <= set(workflow[True])
    commands = " ; ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"]
    )
    for command in ("ruff check", "ruff format --check", "mypy --strict", "pytest"):
        assert command in commands
