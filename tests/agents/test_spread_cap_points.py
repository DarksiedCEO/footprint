"""M3 (b), Andre 2026-10-25: Felix's absolute spread cap in option points.

A quote must pass BOTH spread filters: the spread may be no wider than
max_spread_pct_of_mid of the mid AND no wider than max_spread_points. Equality passes
both, like the percentage filter always did (only "wider than" refuses).
"""

import shutil
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from footprint.agents.contract_picker import ContractChoice, ContractPicker, NoContract
from footprint.core.config import (
    ConfigError,
    ExecutionProfile,
    InstrumentProfile,
    RiskProfile,
    check_consistency,
    load_execution,
    load_profiles,
)
from footprint.core.models import Direction
from footprint.core.options import OptionChain, OptionQuote, Right
from tests.conftest import REPO_ROOT

PT = ZoneInfo("America/Los_Angeles")
D = Decimal
TUE = date(2026, 9, 15)
WED = TUE + timedelta(days=1)
NOW = datetime(2026, 9, 15, 7, 0, tzinfo=PT).astimezone(UTC)


def quote(bid: str, ask: str) -> OptionQuote:
    return OptionQuote(
        contract_id=f"C-{WED}-20000",
        expiry=WED,
        strike=D(20000),
        right=Right.CALL,
        bid=D(bid),
        ask=D(ask),
        open_interest=500,
        trading_class="Q3C",
    )


def choose(picker: ContractPicker, bid: str, ask: str) -> ContractChoice | NoContract:
    chain = OptionChain(
        underlying="NQ", ts=NOW, underlying_price=D(20010), quotes=(quote(bid, ask),)
    )
    return picker.choose(chain, Direction.LONG, NOW)


def with_caps(
    execution: ExecutionProfile, *, points: Decimal | None = None, pct: Decimal | None = None
) -> ExecutionProfile:
    contract = execution.contract
    if points is not None:
        contract = replace(contract, max_spread_points=points)
    if pct is not None:
        contract = replace(contract, max_spread_pct_of_mid=pct)
    return replace(execution, contract=contract)


# --- the shipped draft (M3 (b)) -------------------------------------------------


def test_the_shipped_draft_cap_is_ten_points(execution: ExecutionProfile) -> None:
    assert execution.contract.max_spread_points == D(10)
    assert execution.contract.max_spread_pct_of_mid == D(5)


def test_the_shipped_cap_refuses_the_wide_quote_the_percent_filter_let_through(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    # decisions-pending M3: 288.75/303.50 is 14.75 points, 4.98% of mid: it passed the
    # percentage filter alone at the top of the $6,000 cap.
    result = choose(ContractPicker(execution, instrument), "288.75", "303.50")
    assert isinstance(result, NoContract)
    assert "14.75 points" in result.reason
    assert "> 10 points" in result.reason


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        ("236.50", "244.00"),  # live RTH 29080C, 7.5 points (3.1%)
        ("495.50", "504.50"),  # Oct 30 quote, 9 points on ~500 (1.8%)
    ],
)
def test_the_shipped_cap_accepts_the_live_quotes_seen(
    execution: ExecutionProfile, instrument: InstrumentProfile, bid: str, ask: str
) -> None:
    assert isinstance(choose(ContractPicker(execution, instrument), bid, ask), ContractChoice)


# --- both filters, independent of the shipped numbers ---------------------------


def test_a_spread_over_the_points_cap_is_refused_even_under_the_percent_cap(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    # 6 points on a 300 mid is 2%: far under 5%, but over a 5-point cap.
    picker = ContractPicker(with_caps(execution, points=D(5), pct=D(5)), instrument)
    result = choose(picker, "297", "303")
    assert isinstance(result, NoContract)
    assert result.reason == "C-2026-09-16-20000: spread 6 points > 5 points"


def test_a_spread_over_the_percent_cap_is_refused_even_under_the_points_cap(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    # 6 points on a 100 mid is 6%: under a 50-point cap, over 5%.
    picker = ContractPicker(with_caps(execution, points=D(50), pct=D(5)), instrument)
    result = choose(picker, "97", "103")
    assert isinstance(result, NoContract)
    assert "% of mid" in result.reason


def test_a_spread_under_both_caps_is_accepted(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    picker = ContractPicker(with_caps(execution, points=D(5), pct=D(5)), instrument)
    result = choose(picker, "298", "302")  # 4 points, 1.3%
    assert isinstance(result, ContractChoice)
    assert result.quote.bid == D(298)


@pytest.mark.parametrize(
    ("bid", "ask", "ok"),
    [
        ("297.50", "302.50", True),  # exactly 5 points: equality passes, as for the %
        ("297.25", "302.50", False),  # one option tick wider
        ("297.75", "302.50", True),  # one tick narrower
    ],
)
def test_the_points_boundary_matches_the_percent_filter(
    execution: ExecutionProfile, instrument: InstrumentProfile, bid: str, ask: str, ok: bool
) -> None:
    picker = ContractPicker(with_caps(execution, points=D(5), pct=D(5)), instrument)
    assert isinstance(choose(picker, bid, ask), ContractChoice) is ok


def test_when_both_fail_the_percent_reason_is_given(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    # The order of the checks is fixed: the older, percentage reason comes first.
    picker = ContractPicker(with_caps(execution, points=D(5), pct=D(5)), instrument)
    result = choose(picker, "90", "110")  # 20 points, 20%
    assert isinstance(result, NoContract)
    assert "% of mid" in result.reason


def test_the_points_check_does_not_replace_the_open_interest_check(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    picker = ContractPicker(with_caps(execution, points=D(5)), instrument)
    low_oi = replace(quote("298", "302"), open_interest=execution.contract.min_open_interest - 1)
    chain = OptionChain(underlying="NQ", ts=NOW, underlying_price=D(20010), quotes=(low_oi,))
    result = picker.choose(chain, Direction.LONG, NOW)
    assert isinstance(result, NoContract)
    assert "open interest" in result.reason


# --- the loader fails closed ----------------------------------------------------


def _write(execution_path: Path, tmp_path: Path, value: Any, *, drop: bool = False) -> Path:
    data = yaml.safe_load(execution_path.read_text())
    if drop:
        del data["contract"]["max_spread_points"]
    else:
        data["contract"]["max_spread_points"] = value
    target = tmp_path / "execution.yaml"
    target.write_text(yaml.safe_dump(data))
    return target


@pytest.mark.parametrize(
    "value",
    [0, -1, -0.25, "10", True, False, None, float("nan"), float("inf"), [10], {"v": 10}],
)
def test_the_loader_refuses_a_bad_points_cap(
    execution_path: Path, tmp_path: Path, value: Any
) -> None:
    with pytest.raises(ConfigError, match="max_spread_points"):
        load_execution(_write(execution_path, tmp_path, value))


def test_the_loader_refuses_a_missing_points_cap(execution_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_spread_points"):
        load_execution(_write(execution_path, tmp_path, None, drop=True))


@pytest.mark.parametrize(("value", "expected"), [(7.5, D("7.5")), (12, D(12)), (0.25, D("0.25"))])
def test_the_loader_reads_a_good_points_cap_exactly(
    execution_path: Path, tmp_path: Path, value: float, expected: Decimal
) -> None:
    loaded = load_execution(_write(execution_path, tmp_path, value))
    assert loaded.contract.max_spread_points == expected
    assert isinstance(loaded.contract.max_spread_points, Decimal)


# --- review round 1 (PR #51) ----------------------------------------------------


def _strike(strike: int, bid: str, ask: str) -> OptionQuote:
    return replace(quote(bid, ask), contract_id=f"C-{WED}-{strike}", strike=D(strike))


def test_f1_the_cap_is_judged_on_the_chosen_contract_not_its_neighbours(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    # The nearest strike (20000, the underlying is 20010) is 14.75 points wide (4.98%:
    # under 5%); every neighbour is 6 points wide. Felix must refuse, naming 20000.
    quotes = (
        _strike(19975, "297", "303"),
        _strike(20000, "288.75", "303.50"),
        _strike(20025, "297", "303"),
        _strike(20050, "297", "303"),
    )
    chain = OptionChain(underlying="NQ", ts=NOW, underlying_price=D(20010), quotes=quotes)
    result = ContractPicker(execution, instrument).choose(chain, Direction.LONG, NOW)
    assert isinstance(result, NoContract)
    assert result.reason.startswith(f"C-{WED}-20000: spread 14.75 points")


def test_f1_a_wide_neighbour_does_not_refuse_a_narrow_chosen_contract(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    quotes = (
        _strike(19975, "288.75", "303.50"),
        _strike(20000, "297", "303"),
        _strike(20025, "288.75", "303.50"),
    )
    chain = OptionChain(underlying="NQ", ts=NOW, underlying_price=D(20010), quotes=quotes)
    result = ContractPicker(execution, instrument).choose(chain, Direction.LONG, NOW)
    assert isinstance(result, ContractChoice)
    assert result.quote.contract_id == f"C-{WED}-20000"


def test_f2_the_points_check_comes_before_the_open_interest_check(
    execution: ExecutionProfile, instrument: InstrumentProfile
) -> None:
    # Over the points cap AND under the open-interest floor: the spread reason is given.
    wide_thin = replace(
        quote("288.75", "303.50"), open_interest=execution.contract.min_open_interest - 1
    )
    chain = OptionChain(underlying="NQ", ts=NOW, underlying_price=D(20010), quotes=(wide_thin,))
    result = ContractPicker(execution, instrument).choose(chain, Direction.LONG, NOW)
    assert isinstance(result, NoContract)
    assert "points" in result.reason
    assert "open interest" not in result.reason


# F3: a cap under one option tick would refuse every quote that is not locked.


def test_f3_a_points_cap_under_one_option_tick_is_refused(
    execution: ExecutionProfile, instrument: InstrumentProfile, risk: RiskProfile
) -> None:
    tick = instrument.option_tick_size
    under = with_caps(execution, points=tick - tick / 4)
    with pytest.raises(ConfigError, match="max_spread_points"):
        check_consistency(instrument, risk, under)


def test_f3_a_points_cap_of_exactly_one_option_tick_is_accepted(
    execution: ExecutionProfile, instrument: InstrumentProfile, risk: RiskProfile
) -> None:
    check_consistency(instrument, risk, with_caps(execution, points=instrument.option_tick_size))


def test_f3_the_start_up_path_refuses_a_points_cap_under_one_tick(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    tick = load_profiles(config, "nq.yaml").instrument.option_tick_size
    path = config / "execution.yaml"
    data = yaml.safe_load(path.read_text())
    data["contract"]["max_spread_points"] = float(tick / 2)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="max_spread_points"):
        load_profiles(config, "nq.yaml")
