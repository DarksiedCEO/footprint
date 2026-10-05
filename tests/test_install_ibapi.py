"""The ibapi installer refuses any file but the one pinned by hash."""

import hashlib
import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_ibapi.py"


def installer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_ibapi", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_file_with_another_hash_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = tmp_path / "twsapi.zip"
    with zipfile.ZipFile(fake, "w") as z:
        z.writestr("twsapi_macunix/IBJts/source/pythonclient/setup.py", "raise SystemExit")
    assert installer().main(["install_ibapi.py", str(fake)]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_an_archive_escaping_its_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    evil = tmp_path / "twsapi.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("twsapi_macunix/IBJts/source/pythonclient/../../../../../../escape.py", "x")
    module = installer()
    monkeypatch.setattr(module, "SHA256", hashlib.sha256(evil.read_bytes()).hexdigest())
    assert module.main(["install_ibapi.py", str(evil)]) == 1
    assert "unsafe path" in capsys.readouterr().err


def test_the_pin_names_one_release() -> None:
    module = installer()
    assert module.URL.endswith("twsapi_macunix.1050.02.zip")
    assert len(module.SHA256) == 64
