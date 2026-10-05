"""The ibapi installer refuses any file but the one pinned by hash."""

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
    with zipfile.ZipFile(evil) as z:
        monkeypatch.setattr(module, "TREE_SHA256", module.tree_sha256(z, module.client_files(z)))
    assert module.main(["install_ibapi.py", str(evil)]) == 1
    assert "unsafe path" in capsys.readouterr().err


def test_the_pin_names_one_release() -> None:
    module = installer()
    assert module.URL.endswith("twsapi_macunix.1050.02.zip")
    assert len(module.TREE_SHA256) == 64


def client_zip(path: Path, content: bytes, compression: int) -> zipfile.ZipFile:
    with zipfile.ZipFile(path, "w", compression) as z:
        z.writestr("twsapi_macunix/IBJts/source/pythonclient/ibapi/client.py", content)
        z.writestr("__MACOSX/twsapi_macunix/._x", b"finder noise")  # never installed
    return zipfile.ZipFile(path)


def test_the_pin_is_on_the_files_not_on_the_zip(tmp_path: Path) -> None:
    module = installer()
    stored = client_zip(tmp_path / "a.zip", b"code", zipfile.ZIP_STORED)
    rezipped = client_zip(tmp_path / "b.zip", b"code", zipfile.ZIP_DEFLATED)
    changed = client_zip(tmp_path / "c.zip", b"codE", zipfile.ZIP_STORED)
    pinned = module.tree_sha256(stored, module.client_files(stored))
    assert (tmp_path / "a.zip").read_bytes() != (tmp_path / "b.zip").read_bytes()
    assert module.tree_sha256(rezipped, module.client_files(rezipped)) == pinned
    assert module.tree_sha256(changed, module.client_files(changed)) != pinned
