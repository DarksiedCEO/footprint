"""config/instruments/es.yaml is research only (NB-5): every trading entry point refuses it
(integration-v1, review round 1, L2).

Today the refusal comes from the profile loader's own consistency checks (a stop filled at
its limit would lose more than stop_max at ES's point value), not from a flag that says
"research only". This test pins the outcome, whatever produces it: `load_profiles` with
es.yaml raises ConfigError, and each command that takes `--instrument` refuses it before
any broker connection. If a future change to es.yaml or to the stop numbers made it
loadable, this fails, and the explicit research-only switch (an open option for Andre)
has to be decided first.

The research harness reads es.yaml with `load_instrument` only; that is not a trading
path and is tested in tests/research.

The commands that import IBKR's ibapi (the live runner, check, record_chain, history)
run in a child process with a stand-in ibapi whose every class, if ever built, ends the
child with a marker, and with socket connects blocked the same way: so a refusal
"before any broker connection" is checked here, where the real ibapi is not installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from footprint.backtest.__main__ import main as backtest_main
from footprint.core.config import ConfigError, load_profiles
from footprint.live.kill_switch import main as kill_switch_main
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config"
ES = "es.yaml"
TOUCHED = "BROKER TOUCHED"
_TOUCHED_EXIT = 97

_IBAPI_MODULES = (
    "client",
    "common",
    "const",
    "contract",
    "execution",
    "order",
    "order_cancel",
    "order_state",
    "ticktype",
    "wrapper",
)

_STUB_INIT = textwrap.dedent(
    f"""
    import os
    import sys


    def touched(*args, **kwargs):
        print({TOUCHED!r}, flush=True)
        os._exit({_TOUCHED_EXIT})


    class _Meta(type):
        def __getattr__(cls, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return cls


    _made = {{}}


    def stub(name):
        if name not in _made:
            _made[name] = _Meta(name, (), {{"__init__": touched}})
        return _made[name]
    """
)

_STUB_MODULE = textwrap.dedent(
    """
    import ibapi


    def __getattr__(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return ibapi.stub(__name__ + "." + name)
    """
)

_RUNNER = textwrap.dedent(
    """
    import importlib
    import socket
    import sys

    import ibapi

    socket.socket.connect = ibapi.touched
    socket.socket.connect_ex = ibapi.touched
    socket.create_connection = ibapi.touched

    from footprint.core.config import ConfigError

    module = importlib.import_module(sys.argv[1])
    try:
        code = module.main(sys.argv[2:])
    except ConfigError as exc:
        print("CONFIG_REFUSED", exc, flush=True)
        code = "config"
    except SystemExit as exc:
        code = exc.code
    print("EXIT", code, flush=True)
    """
)


def _stub_ibapi(root: Path) -> Path:
    package = root / "ibapi"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(_STUB_INIT, encoding="utf-8")
    for name in _IBAPI_MODULES:
        (package / f"{name}.py").write_text(_STUB_MODULE, encoding="utf-8")
    (root / "runner.py").write_text(_RUNNER, encoding="utf-8")
    return root


def _child(tmp: Path, module: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    stub = _stub_ibapi(tmp / "stub")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(stub), str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, str(stub / "runner.py"), module, *argv],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# --- the loader ----------------------------------------------------------------------------


def test_the_profile_loader_refuses_es() -> None:
    with pytest.raises(ConfigError):
        load_profiles(CONFIG, ES)


def test_the_profile_loader_accepts_nq() -> None:
    """The control: the same loader, the same config directory, the trading profile."""
    assert load_profiles(CONFIG, "nq.yaml").instrument.symbol == "NQ"


# --- the commands that need no ibapi -----------------------------------------------------


def test_the_kill_switch_refuses_es_before_touching_the_ledger(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with pytest.raises(ConfigError):
        kill_switch_main(
            ["--config", str(CONFIG), "--instrument", ES, "--state", str(state), "status"]
        )
    assert not state.exists()


def test_the_backtest_refuses_es_before_reading_bars(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(ConfigError):
        backtest_main(
            [
                "--bars",
                str(tmp_path / "no-such-bars.csv"),
                "--out",
                str(out),
                "--config",
                str(CONFIG),
                "--instrument",
                ES,
            ]
        )
    assert not out.exists()


# --- the commands that connect to IB Gateway ---------------------------------------------


def test_the_stand_in_ibapi_catches_a_broker_connection(tmp_path: Path) -> None:
    """The control for the checks below: building the gateway's ibapi client in the
    child is caught (so a refusal that comes after it would be seen)."""
    stub = _stub_ibapi(tmp_path / "stub")
    script = "import ibapi.client\nibapi.client.EClient(None)\n"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(stub), env.get("PYTHONPATH", "")])
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == _TOUCHED_EXIT
    assert TOUCHED in done.stdout


def test_the_live_runner_refuses_es_before_any_broker_connection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    done = _child(
        tmp_path,
        "footprint.live.__main__",
        [
            "--config",
            str(CONFIG),
            "--instrument",
            ES,
            "--no-blackouts",
            "--env",
            str(tmp_path / ".env"),
            "--state",
            str(state),
        ],
    )
    assert done.returncode == 0, done.stderr
    assert TOUCHED not in done.stdout
    assert "EXIT 2" in done.stdout
    assert "REFUSED:" in done.stderr
    assert not state.exists()


def test_the_check_refuses_es_before_any_broker_connection(tmp_path: Path) -> None:
    done = _child(
        tmp_path,
        "footprint.brokers.ibkr.check",
        ["--config", str(CONFIG), "--instrument", ES, "--env", str(tmp_path / ".env")],
    )
    assert done.returncode == 0, done.stderr
    assert TOUCHED not in done.stdout
    assert "CONFIG_REFUSED" in done.stdout
    assert "EXIT config" in done.stdout


@pytest.mark.parametrize(
    ("module", "out"),
    [
        ("footprint.brokers.ibkr.record_chain", "chains"),
        ("footprint.brokers.ibkr.history", "bars.csv"),
    ],
)
def test_the_recorders_refuse_es_before_any_broker_connection(
    tmp_path: Path, module: str, out: str
) -> None:
    target = tmp_path / out
    done = _child(
        tmp_path,
        module,
        [
            "--out",
            str(target),
            "--config",
            str(CONFIG),
            "--instrument",
            ES,
            "--env",
            str(tmp_path / ".env"),
        ],
    )
    assert done.returncode == 0, done.stderr
    assert TOUCHED not in done.stdout
    assert "EXIT 2" in done.stdout  # argparse's error exit: the loader's ConfigError
    assert "error:" in done.stderr
    assert not target.exists()
