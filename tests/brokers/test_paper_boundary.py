"""The paper-account boundary is hard-coded: no .env value, YAML key or command-line
argument can admit a live account or a live port (AEGIS H1 remediation, D33)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from footprint.brokers.ibkr.binding import IbBinding, IbConnectionSettings, read_env_file
from footprint.brokers.ibkr.paper import (
    LIVE_PORTS,
    LiveAccountRefused,
    check_paper,
    check_paper_login,
    is_paper_account,
)
from footprint.core.config import ConfigError, load_profiles
from tests.brokers.test_ibkr_binding import ACCOUNT, Recording, ibkr_config, settings
from tests.conftest import REPO_ROOT

LIVE_ACCOUNTS = ("U1234567", "F1234567", "I1234567", "DF1234567", "du1234567", "XDU123", "DU")
LIVE_PORT_LIST = (4001, 7496)


@pytest.mark.parametrize("account", ["DU1234567", "DU0000000", "DUR123456"])
def test_paper_accounts_are_accepted(account: str) -> None:
    assert is_paper_account(account)
    check_paper(account, 4002)


@pytest.mark.parametrize("account", LIVE_ACCOUNTS)
def test_every_non_paper_account_is_refused(account: str) -> None:
    assert not is_paper_account(account)
    with pytest.raises(LiveAccountRefused, match="paper account only"):
        check_paper(account, 4002)


@pytest.mark.parametrize("port", LIVE_PORT_LIST)
def test_the_live_ports_are_refused_even_with_a_paper_account(port: int) -> None:
    assert port in LIVE_PORTS
    with pytest.raises(LiveAccountRefused, match="live port"):
        check_paper(ACCOUNT, port)


def test_unicode_lookalikes_are_refused() -> None:
    for account in (
        "DU" + "\uff11\uff12\uff13\uff14\uff15\uff16\uff17",
        "DU1234567\n",
        " DU1234567",
    ):
        assert not is_paper_account(account)


def test_a_login_managing_any_live_account_is_refused() -> None:
    check_paper_login([ACCOUNT, "DU7654321"])
    with pytest.raises(LiveAccountRefused, match="live login"):
        check_paper_login([ACCOUNT, "U7654321"])


# --- configuration cannot bypass it ---------------------------------------------------------


@pytest.mark.parametrize(
    ("account", "port"),
    [*((a, 4002) for a in LIVE_ACCOUNTS), *((ACCOUNT, p) for p in LIVE_PORT_LIST)],
)
def test_the_env_file_cannot_make_live_settings(tmp_path: Path, account: str, port: int) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"BROKER_HOST=127.0.0.1\nBROKER_PORT={port}\nBROKER_ACCOUNT_ID={account}\n"
        "BROKER_CLIENT_ID=0\nREQUIRE_PAPER=false\nPAPER_ACCOUNT_PREFIX=U\n"
    )
    with pytest.raises(LiveAccountRefused):
        IbConnectionSettings.from_env(read_env_file(str(env)), ibkr_config())


def test_settings_cannot_be_made_with_a_live_account_or_port() -> None:
    with pytest.raises(LiveAccountRefused):
        settings(account="U1234567")
    with pytest.raises(LiveAccountRefused):
        settings(port=4001)


@pytest.mark.parametrize(
    "key",
    [
        {"require_paper": False},
        {"paper_account_prefix": "U"},
        {"live": True},
        {"allow_live_port": 4001},
    ],
)
def test_no_yaml_key_can_switch_paper_off(tmp_path: Path, key: dict[str, object]) -> None:
    config = tmp_path / "config"
    config.mkdir()
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


def test_no_setting_anywhere_names_the_paper_rule() -> None:
    """The rule lives in code only: no config file mentions a way to turn it off."""
    for f in (REPO_ROOT / "config").rglob("*.yaml"):
        text = f.read_text()
        for word in ("require_paper:", "paper_account_prefix:", "allow_live"):
            assert word not in text, f"{f}: {word}"


def test_the_command_lines_have_no_live_switch() -> None:
    """No argparse option in the programs mentions live, paper or a prefix."""
    for path in (REPO_ROOT / "src" / "footprint").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        lowered = arg.value.lower()
                        assert "live" not in lowered or "local" in lowered, (path, arg.value)
                        assert "paper" not in lowered, (path, arg.value)
                        assert "prefix" not in lowered, (path, arg.value)


# --- start-up fails closed ---------------------------------------------------------------------


def _bypassed(account: str, port: int) -> IbConnectionSettings:
    """Settings forced past their own check (as a bug or a hand edit might)."""
    from dataclasses import fields

    good = settings()
    forced = object.__new__(IbConnectionSettings)
    for f in fields(IbConnectionSettings):
        object.__setattr__(forced, f.name, getattr(good, f.name))
    object.__setattr__(forced, "account", account)
    object.__setattr__(forced, "port", port)
    return forced


@pytest.mark.parametrize(("account", "port"), [("U1234567", 4002), (ACCOUNT, 4001)])
def test_the_binding_never_connects_with_forced_live_settings(account: str, port: int) -> None:
    import threading

    clients: list[Recording] = []
    alarms: list[str] = []

    def factory(wrapper: object) -> Recording:
        client = Recording(wrapper)
        clients.append(client)
        return client

    binding = IbBinding(_bypassed(account, port), ("NQ", "FOP"), alarms.append, factory)
    stop = threading.Event()
    binding.run(stop)  # returns at once: refused before any connection
    assert clients == []
    assert binding.refused is not None
    assert any("IBKR CONNECTION REFUSED" in a for a in alarms)


def test_the_binding_refuses_a_live_account_reported_by_the_login() -> None:
    """Belt and braces: a forced live account that reaches managedAccounts is refused."""
    alarms: list[str] = []
    binding = IbBinding(_bypassed("U1234567", 4002), ("NQ", "FOP"), alarms.append, Recording)
    binding._wrapper.managedAccounts("U1234567")
    assert binding.refused is not None


def test_the_live_program_refuses_to_start_with_a_live_account_or_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from footprint.live.__main__ import main

    for account, port in (("U1234567", 4002), (ACCOUNT, 4001)):
        env = tmp_path / ".env"
        env.write_text(
            f"BROKER_HOST=127.0.0.1\nBROKER_PORT={port}\nBROKER_ACCOUNT_ID={account}\n"
            "BROKER_CLIENT_ID=0\n"
        )
        code = main(
            [
                "--env",
                str(env),
                "--no-blackouts",
                "--state",
                str(tmp_path / "s"),
                "--alarms-local-only",
                "--new-ledger",
            ]
        )
        assert code == 2
        assert "REFUSED" in capsys.readouterr().err
        assert not (tmp_path / "s" / "ledger.json").exists()


def test_the_read_only_check_refuses_a_live_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from footprint.brokers.ibkr.check import main

    env = tmp_path / ".env"
    env.write_text(
        "BROKER_HOST=127.0.0.1\nBROKER_PORT=4001\nBROKER_ACCOUNT_ID=DU1234567\nBROKER_CLIENT_ID=0\n"
    )
    assert main(["--env", str(env), "--config", str(REPO_ROOT / "config")]) == 2
    assert "live port" in capsys.readouterr().err
