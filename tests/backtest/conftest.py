import csv
import re
import shutil
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.settings import BacktestSettings, load_backtest
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME, market

D = Decimal
MINUTE = timedelta(minutes=1)
TRADE_FROM = DECISION_TIME - MINUTE  # earlier bars are the readers' look-back only


@pytest.fixture(scope="session")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="session")
def shipped_settings() -> BacktestSettings:
    """config/backtest.yaml as shipped: option prices rescaled to reference_price (BH-2)."""
    return load_backtest(REPO_ROOT / "config" / "backtest.yaml")


@pytest.fixture(scope="session")
def settings(shipped_settings: BacktestSettings) -> BacktestSettings:
    """The shipped settings with price-level rescaling OFF (reference_price null): the
    option model exactly as it was before backtest-history-v1. The engine tests written
    before it pin that model; the rescaled one has its own tests
    (test_price_level.py, test_backtest_history.py)."""
    model = replace(shipped_settings.option_model, reference_price=None)
    # and with roll sessions not blocked (BH-9): those tests' bars have no contract roll
    rolls = replace(shipped_settings.rolls, block_first_session=False)
    return replace(shipped_settings, option_model=model, rolls=rolls)


_REFERENCE_LINE = re.compile(r"^(  reference_price: )\S+", re.MULTILINE)


_ROLLS_LINE = re.compile(r"^(  block_first_session: )\S+", re.MULTILINE)


def config_copy(target: Path, reference: Decimal | None, *, block_rolls: bool = False) -> Path:
    """A copy of config/ with option_model.reference_price set to `reference` (None:
    rescaling off) and rolls.block_first_session set to `block_rolls` (off unless asked:
    the tests' bars have no contract roll); every other byte is the shipped file's."""
    shutil.copytree(REPO_ROOT / "config", target)
    path = target / "backtest.yaml"
    text, n = _REFERENCE_LINE.subn(
        lambda m: m.group(1) + ("null" if reference is None else str(reference)),
        path.read_text(encoding="utf-8"),
    )
    assert n == 1, "backtest.yaml must carry exactly one reference_price line"
    text, n = _ROLLS_LINE.subn(lambda m: m.group(1) + str(block_rolls).lower(), text)
    assert n == 1, "backtest.yaml must carry exactly one block_first_session line"
    path.write_text(text, encoding="utf-8")
    return target


def write_csv(path: Path, bars: Sequence[Bar]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])


@pytest.fixture(scope="session")
def history() -> tuple[Bar, ...]:
    return tuple(market())  # fires a long entry at DECISION_TIME (07:01 PT)


ENTRY_DIP = D(8)  # reached by second ~25 even on the worst-case (high-first) path


def extend(
    history: Sequence[Bar], moves: Sequence[Decimal], *, dip: Decimal = ENTRY_DIP
) -> list[Bar]:
    """The scenario, then one bar per move from DECISION_TIME on. The first bar dips
    `dip` points below its open first, so the resting entry fills."""
    bars = list(history)
    price = bars[-1].close
    for i, move in enumerate(moves):
        o, c = price, price + move
        low = min(o, c) - (dip if i == 0 else D(1))
        bars.append(Bar("NQ", DECISION_TIME + MINUTE * i, o, max(o, c) + 1, low, c, 100))
        price = c
    return bars


def workdir(tmp_path: Path) -> Path:
    return tmp_path / "run"
