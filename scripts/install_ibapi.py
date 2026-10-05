"""Install IBKR's official Python API (ibapi), pinned by hash, into this project's
environment.

IBKR does not publish ibapi on PyPI (the `ibapi` there is an old unofficial upload),
so it comes from IBKR's own download, checked against the SHA-256 recorded below:

    uv run python scripts/install_ibapi.py                 # downloads from IBKR
    uv run python scripts/install_ibapi.py path/to/zip     # a copy you downloaded

A different file is refused. `uv sync` removes ibapi (it is not in the lock): run this
again after any `uv sync`. `uv run` leaves it in place.

To move to another IBKR release, change VERSION, URL and SHA256 together, in a
reviewed change.

ibapi is GPL-3.0-or-later (see docs/decisions-pending.md, D25).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "10.50.2"  # ibapi's own version string for IBKR "Stable" API 10.50.02 (2026-09-09)
URL = "https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.02.zip"
SHA256 = "0d6482032f17934025039623fa85dee6d0651727ba73d3dac977d8c50ebccedd"
CLIENT = "twsapi_macunix/IBJts/source/pythonclient/"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if len(argv) > 1:
            archive = Path(argv[1])
        else:
            archive = work / "twsapi.zip"
            print(f"downloading {URL}")
            with urllib.request.urlopen(URL, timeout=120) as response, archive.open("wb") as f:
                shutil.copyfileobj(response, f)
        actual = _sha256(archive)
        if actual != SHA256:
            print(f"REFUSED: {archive} has SHA-256 {actual}, expected {SHA256}", file=sys.stderr)
            return 1
        with zipfile.ZipFile(archive) as z:
            members = [m for m in z.namelist() if m.startswith(CLIENT)]
            for member in members:
                target = (work / member).resolve()
                if not target.is_relative_to(work.resolve()):
                    print(f"REFUSED: unsafe path in archive: {member}", file=sys.stderr)
                    return 1
            z.extractall(work, members)
        source = work / CLIENT
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
        print(f"REFUSED: installed ibapi {installed}, expected {VERSION}", file=sys.stderr)
        return 1
    print(f"ibapi {installed} installed (SHA-256 verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
