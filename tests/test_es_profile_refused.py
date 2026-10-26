"""config/instruments/es.yaml is research only (NB-5): every trading entry point refuses it
(integration-v1, review rounds 1 and 2: L2, R2-1, R2-2).

Today the refusal comes from the profile loader's own consistency check: a stop filled
at its limit would lose more than stop_max at ES's point value ("... over stop_max ...",
core/config.py). There is no flag that says "research only". These tests pin that exact
refusal at the loader and at each command that takes `--instrument`, and that it comes
before any broker connection. Each command is also run with nq.yaml, the same argv, as
the control: that run must NOT show the refusal, so the es.yaml refusal is the
instrument's, not some other start-up stop. If a future change to es.yaml or to the
stop numbers made it loadable, these fail, and the explicit research-only switch (an
open option for Andre) has to be decided first.

The research harness reads es.yaml with `load_instrument` only; that is not a trading
path and is tested in tests/research.

The commands that import IBKR's ibapi (the live runner, check, record_chain, history)
run in a child process with a stand-in ibapi whose every class, if ever built, ends the
child with a marker (exit 97), with socket connects blocked the same way, and with a
filled stand-in .env (a paper DU account, nothing real), so the profile is the only
thing that can stop the es.yaml run. The live runner's stand-in .env also carries six
dummy alarm keys (fake values in the right format; nothing is sent, every socket connect
ends the child), so it gets past its alarm set-up too. The es.yaml live run is made both
without and with those keys: without them, a profile check moved after the alarm set-up
would show the alarm refusal, not the stop_max one (PR #50 review F1). Under nq.yaml all
four commands reach the stand-in (the harness is live), so under es.yaml the profile is
the only stop (review R3-A).
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
NQ = "nq.yaml"
# The profile loader's stop_max consistency refusal (core/config.py check_consistency).
REFUSAL = "over stop_max"
LIVE = "footprint.live.__main__"
CHECK = "footprint.brokers.ibkr.check"
RECORD_CHAIN = "footprint.brokers.ibkr.record_chain"
HISTORY = "footprint.brokers.ibkr.history"
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


# Six alarm keys in the formats the live runner checks, none of them real (R3-A): with
# them the runner gets past its alarm set-up, so only the profile can stop it first.
_DUMMY_ALARMS = (
    "TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=1\n"
    "TWILIO_ACCOUNT_SID=AC00000000000000000000000000000000\nTWILIO_AUTH_TOKEN=x\n"
    "TWILIO_FROM_NUMBER=+15550100001\nALARM_PHONE_NUMBER=+15550100002\n"
)


def _env(tmp: Path, *, alarms: bool = False) -> Path:
    """A filled stand-in .env: a paper DU account id that is not a real one, plus dummy
    alarm keys when `alarms` is set (the live runner)."""
    path = tmp / ".env"
    path.write_text(
        "BROKER_HOST=127.0.0.1\nBROKER_PORT=4002\nBROKER_ACCOUNT_ID=DU0000000\n"
        "BROKER_CLIENT_ID=0\n" + (_DUMMY_ALARMS if alarms else ""),
        encoding="utf-8",
    )
    return path


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


def _argv(module: str, tmp: Path, instrument: str, *, alarms: bool = False) -> list[str]:
    env = _env(tmp, alarms=alarms)
    common = ["--config", str(CONFIG), "--instrument", instrument, "--env", str(env)]
    if module == LIVE:
        return [*common, "--no-blackouts", "--state", str(tmp / "state")]
    if module == RECORD_CHAIN:
        return [*common, "--out", str(tmp / "chains")]
    if module == HISTORY:
        return [*common, "--out", str(tmp / "bars.csv")]
    return common


# --- the loader ----------------------------------------------------------------------------


def test_the_profile_loader_refuses_es_for_its_stop() -> None:
    with pytest.raises(ConfigError, match=REFUSAL):
        load_profiles(CONFIG, ES)


def test_the_profile_loader_accepts_nq() -> None:
    """The control: the same loader, the same config directory, the trading profile."""
    assert load_profiles(CONFIG, NQ).instrument.symbol == "NQ"


# --- the commands that need no ibapi -----------------------------------------------------


def _kill_switch(tmp: Path, instrument: str) -> None:
    kill_switch_main(
        ["--config", str(CONFIG), "--instrument", instrument, "--state", str(tmp / "s"), "status"],
        say=lambda _: None,
    )


def _backtest(tmp: Path, instrument: str) -> None:
    out = tmp / "out"
    backtest_main(
        [
            "--bars",
            str(tmp / "no-such-bars.csv"),
            "--out",
            str(out),
            "--config",
            str(CONFIG),
            "--instrument",
            instrument,
        ]
    )


_IN_PROCESS = {"kill_switch": _kill_switch, "backtest": _backtest}


@pytest.mark.parametrize("name", sorted(_IN_PROCESS))
def test_the_in_process_commands_refuse_es_for_its_stop(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match=REFUSAL):
        _IN_PROCESS[name](tmp_path, ES)
    assert not (tmp_path / "s").exists()  # the kill switch never opened a ledger
    assert not (tmp_path / "out").exists()  # the backtest never wrote a result


def test_with_nq_the_kill_switch_gets_past_the_profile(tmp_path: Path) -> None:
    """The control: the same argv with nq.yaml loads and reports (no refusal at all)."""
    _kill_switch(tmp_path, NQ)


def test_with_nq_the_backtest_gets_past_the_profile(tmp_path: Path) -> None:
    """The control: the same argv with nq.yaml loads the profile and stops only at the
    missing bars file, never with the stop_max refusal."""
    with pytest.raises(FileNotFoundError, match="no-such-bars"):
        _backtest(tmp_path, NQ)


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


@pytest.mark.parametrize("alarms", [False, True], ids=["no-alarm-keys", "dummy-alarm-keys"])
def test_the_live_runner_refuses_es_for_its_stop(tmp_path: Path, alarms: bool) -> None:
    """Both ways (PR #50 review F1): without alarm keys, a profile check moved after the
    alarm set-up would print the alarm refusal instead, so the order is pinned; with the
    dummy keys, nothing but the profile can stop the run before the broker."""
    done = _child(tmp_path, LIVE, _argv(LIVE, tmp_path, ES, alarms=alarms))
    assert done.returncode == 0, done.stderr
    assert TOUCHED not in done.stdout
    assert "EXIT 2" in done.stdout
    assert "REFUSED: " in done.stderr
    assert REFUSAL in done.stderr
    assert not (tmp_path / "state").exists()


def test_the_check_refuses_es_for_its_stop(tmp_path: Path) -> None:
    done = _child(tmp_path, CHECK, _argv(CHECK, tmp_path, ES))
    assert done.returncode == 0, done.stderr
    assert TOUCHED not in done.stdout
    assert "CONFIG_REFUSED" in done.stdout
    assert REFUSAL in done.stdout
    assert "EXIT config" in done.stdout


@pytest.mark.parametrize(("module", "out"), [(RECORD_CHAIN, "chains"), (HISTORY, "bars.csv")])
def test_the_recorders_refuse_es_for_its_stop(tmp_path: Path, module: str, out: str) -> None:
    done = _child(tmp_path, module, _argv(module, tmp_path, ES))
    assert done.returncode == 0, done.stderr
    assert TOUCHED not in done.stdout
    assert "EXIT 2" in done.stdout  # argparse's error exit, with the loader's message
    assert REFUSAL in done.stderr
    assert not (tmp_path / out).exists()


@pytest.mark.parametrize("module", [CHECK, RECORD_CHAIN, HISTORY])
def test_with_nq_the_broker_commands_reach_the_stand_in_broker(tmp_path: Path, module: str) -> None:
    """The control (R2-2): the same argv with nq.yaml gets past the profile and builds
    the broker client, which the stand-in catches. So the harness is live, and the es.yaml
    runs above stop before that point because of the profile alone."""
    done = _child(tmp_path, module, _argv(module, tmp_path, NQ))
    assert done.returncode == _TOUCHED_EXIT, (done.stdout, done.stderr)
    assert TOUCHED in done.stdout
    assert REFUSAL not in done.stdout + done.stderr


def test_with_nq_the_live_runner_reaches_the_stand_in_broker(tmp_path: Path) -> None:
    """The control (R3-A): with nq.yaml and the dummy alarm keys, the live runner gets past
    the profile and its alarm set-up and reaches the stand-in. So the es.yaml live run
    above stops at the profile alone, before any broker or alarm connection."""
    done = _child(tmp_path, LIVE, _argv(LIVE, tmp_path, NQ, alarms=True))
    assert done.returncode == _TOUCHED_EXIT, (done.stdout, done.stderr)
    assert TOUCHED in done.stdout
    assert REFUSAL not in done.stdout + done.stderr
    assert "phone alarms are not set up" not in done.stderr
