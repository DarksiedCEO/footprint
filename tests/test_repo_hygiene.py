import shutil
import subprocess

import pytest

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
