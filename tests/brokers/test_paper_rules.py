"""The paper boundary's pure rules, testable WITHOUT ibapi (independent audit A-13).

tests/brokers/test_paper_boundary.py also covers the binding and the live program, so
it needs IBKR's ibapi. These tests import only `paper.py`, the connection settings and
the config loader, so the DU rule, the live ports and every config route are exercised
wherever the suite runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from footprint.brokers.ibkr.paper import (
    LIVE_PORTS,
    LiveAccountRefused,
    check_paper,
    check_paper_login,
    is_paper_account,
)
from footprint.brokers.ibkr.settings import IbConnectionSettings, read_env_file
from footprint.core.config import ConfigError, IbkrConfig, load_profiles
from tests.conftest import REPO_ROOT

PAPER = "DU1234567"
LIVE_ACCOUNTS = (
    "U1234567",
    "F1234567",
    "I1234567",
    "DF1234567",
    "du1234567",
    "Du1234567",
    "XDU123",
    "DU",
    "DU_1",
    "DU1,U2",
    "",
)


def _ibkr() -> IbkrConfig:
    return load_profiles(REPO_ROOT / "config", "nq.yaml").execution.ibkr


@pytest.mark.parametrize("account", [PAPER, "DU0000000", "DUR123456"])
def test_paper_accounts_are_accepted(account: str) -> None:
    assert is_paper_account(account)
    check_paper(account, 4002)


@pytest.mark.parametrize("account", LIVE_ACCOUNTS)
def test_every_non_paper_account_is_refused(account: str) -> None:
    assert not is_paper_account(account)
    with pytest.raises(LiveAccountRefused, match="paper account only"):
        check_paper(account, 4002)


@pytest.mark.parametrize("port", [4001, 7496])
def test_the_live_ports_are_refused_even_with_a_paper_account(port: int) -> None:
    assert port in LIVE_PORTS
    with pytest.raises(LiveAccountRefused, match="live port"):
        check_paper(PAPER, port)


def test_the_paper_gateway_port_is_accepted() -> None:
    check_paper(PAPER, 4002)


def test_lookalikes_and_padding_are_refused() -> None:
    for account in (
        "DU" + "\uff11\uff12\uff13",  # fullwidth digits
        "\uff24\uff35123",  # fullwidth D U
        "DU123\u200b",  # zero-width space
        "DU1234567\n",
        " DU1234567",
        "DU1234567 ",
        "DU\u0661\u0662",  # Arabic-Indic digits
    ):
        assert not is_paper_account(account), repr(account)


def test_a_login_managing_any_live_account_is_refused() -> None:
    check_paper_login([PAPER, "DU7654321"])
    for accounts in ([PAPER, "U7654321"], [PAPER, ""], [PAPER, " U2"]):
        with pytest.raises(LiveAccountRefused, match="live login"):
            check_paper_login(accounts)


@pytest.mark.parametrize(
    ("account", "port"),
    [*((a, 4002) for a in LIVE_ACCOUNTS if a), *((PAPER, p) for p in (4001, 7496))],
)
def test_the_env_file_cannot_make_live_settings(tmp_path: Path, account: str, port: int) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"BROKER_HOST=127.0.0.1\nBROKER_PORT={port}\nBROKER_ACCOUNT_ID={account}\n"
        "BROKER_CLIENT_ID=0\nREQUIRE_PAPER=false\nPAPER_ACCOUNT_PREFIX=U\n"
    )
    with pytest.raises(LiveAccountRefused):
        IbConnectionSettings.from_env(read_env_file(str(env)), _ibkr())


def test_a_port_written_another_way_is_still_refused(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"BROKER_HOST=127.0.0.1\nBROKER_PORT=4_001\nBROKER_ACCOUNT_ID={PAPER}\nBROKER_CLIENT_ID=0\n"
    )
    with pytest.raises(LiveAccountRefused):
        IbConnectionSettings.from_env(read_env_file(str(env)), _ibkr())


def test_paper_settings_are_made(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"# paper\nBROKER_HOST=127.0.0.1\nBROKER_PORT=4002\nBROKER_ACCOUNT_ID={PAPER}\n"
        "BROKER_CLIENT_ID=0\n"
    )
    settings = IbConnectionSettings.from_env(read_env_file(str(env)), _ibkr())
    assert (settings.account, settings.port, settings.client_id) == (PAPER, 4002, 0)


def test_settings_cannot_be_built_directly_with_a_live_account_or_port() -> None:
    ibkr = _ibkr()
    numbers = (
        float(ibkr.reconnect_seconds),
        ibkr.escalate_after_failures,
        float(ibkr.max_reconnect_seconds),
        float(ibkr.min_healthy_session_seconds),
    )
    for account, port in (("U1234567", 4002), (PAPER, 4001), (PAPER, 7496)):
        with pytest.raises(LiveAccountRefused):
            IbConnectionSettings("127.0.0.1", port, 0, account, *numbers)


@pytest.mark.parametrize(
    "key",
    [
        {"require_paper": False},
        {"paper_account_prefix": "U"},
        {"live": True},
        {"allow_live_port": 4001},
        {"port": 4001},
        {"account": "U1234567"},
    ],
)
def test_no_yaml_key_can_switch_paper_off(tmp_path: Path, key: dict[str, object]) -> None:
    config = tmp_path / "config"
    for f in (REPO_ROOT / "config").rglob("*.yaml"):
        target = config / f.relative_to(REPO_ROOT / "config")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.read_text())
    execution = config / "execution.yaml"
    raw = yaml.safe_load(execution.read_text())
    raw["ibkr"].update(key)
    execution.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_profiles(config, "nq.yaml")


def test_the_settings_module_never_imports_ibapi_or_the_binding() -> None:
    path = REPO_ROOT / "src" / "footprint" / "brokers" / "ibkr" / "settings.py"
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m.startswith("ibapi") or m.endswith(".binding") for m in imported)
