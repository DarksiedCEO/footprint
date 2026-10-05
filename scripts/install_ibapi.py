"""Install IBKR's official Python API (ibapi), pinned by hash, into this project's
environment.

IBKR does not publish ibapi on PyPI (the `ibapi` there is an old unofficial upload),
so it comes from IBKR's own download. What is pinned is the Python client itself: a
SHA-256 over every file under pythonclient/ (path and content), so a copy that was
unzipped and zipped again on a Mac (which changes the zip, not the files) is
accepted, and any change to a single file is refused:

    uv run python scripts/install_ibapi.py                 # downloads from IBKR
    uv run python scripts/install_ibapi.py path/to/zip     # a copy you downloaded

A different file is refused. `uv sync` removes ibapi (it is not in the lock): run this
again after any `uv sync`. `uv run` leaves it in place.

To move to another IBKR release, change VERSION, URL and TREE_SHA256 together, in a
reviewed change.

ibapi is GPL-3.0-or-later (see docs/decisions-pending.md, D25).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "10.50.2"  # ibapi's own version string for IBKR "Stable" API 10.50.02 (2026-09-09)
URL = "https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.02.zip"
TREE_SHA256 = "24ce67883549a200867eef0c535fcc951718b43a1f448b9e5ed6a9e1bc76652c"
CLIENT = "IBJts/source/pythonclient/"  # under whatever folder the zip was made from


def _fail(message: str) -> int:
    """Report a refusal (as an annotation too, when running in GitHub Actions)."""
    print(f"REFUSED: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=install_ibapi::{message}")
    return 1


def client_root(archive: zipfile.ZipFile) -> str | None:
    """The one folder in the zip ending in IBJts/source/pythonclient/ (IBKR's zip and a
    copy zipped again on a Mac put it under different folders); None unless exactly one."""
    roots = {
        m[: m.index(CLIENT) + len(CLIENT)]
        for m in archive.namelist()
        if CLIENT in m and not m.startswith("__MACOSX/")
    }
    return roots.pop() if len(roots) == 1 else None


def client_files(archive: zipfile.ZipFile) -> list[str]:
    root = client_root(archive)
    if root is None:
        return []
    return sorted(m for m in archive.namelist() if m.startswith(root) and not m.endswith("/"))


def tree_sha256(archive: zipfile.ZipFile, members: list[str]) -> str:
    """One hash over each file's path (inside pythonclient/) and content."""
    root = client_root(archive) or ""
    digest = hashlib.sha256()
    for member in members:
        content = hashlib.sha256(archive.read(member)).hexdigest()
        digest.update(f"{member[len(root) :]}\0{content}\n".encode())
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if len(argv) > 1:
            archive = Path(argv[1])
        else:
            archive = work / "twsapi.zip"
            print(f"downloading {URL}")
            try:
                with (
                    urllib.request.urlopen(URL, timeout=120) as response,
                    archive.open("wb") as f,
                ):
                    shutil.copyfileobj(response, f)
            except OSError as exc:
                return _fail(f"could not download {URL}: {exc!r}")
        with zipfile.ZipFile(archive) as z:
            members = client_files(z)
            if not members:
                return _fail(f"{archive.name} has no single {CLIENT}")
            actual = tree_sha256(z, members)
            if actual != TREE_SHA256:
                return _fail(
                    f"the Python client in {archive.name} has SHA-256 {actual}, "
                    f"expected {TREE_SHA256}"
                )
            for member in members:
                target = (work / member).resolve()
                if not target.is_relative_to(work.resolve()):
                    return _fail(f"unsafe path in archive: {member}")
            z.extractall(work, members)
        source = work / members[0][: members[0].index(CLIENT) + len(CLIENT)]
        # Nothing is fetched here: protobuf (ibapi's one dependency) and setuptools
        # (which builds it) are locked, with hashes, in the "ibkr" dependency group;
        # --no-build-isolation builds with that locked setuptools, and --offline makes
        # any attempt to fetch fail.
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--no-build-isolation",
                "--python",
                sys.executable,
                str(source),
            ],
            check=True,
        )
    import importlib

    installed = importlib.import_module("ibapi").get_version_string()
    if installed != VERSION:
        return _fail(f"installed ibapi {installed}, expected {VERSION}")
    print(f"ibapi {installed} installed (SHA-256 verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
