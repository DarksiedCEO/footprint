from dataclasses import replace

import pytest

from footprint.research.exits import Fill, OptionExit
from footprint.research.settings import ExitSettings, ResearchSettings

# Entry option value 100 points, $20 a point, $170 round trip a contract.
# stop: gross -$750 -> value 62.5. lock: net peak >= $1,000 (value 158.5) locks 50%.
EX = ExitSettings(
    stop_dollars=750.0,
    lock_start_dollars=1000.0,
    lock_fraction=0.5,
    first_target_dollars=1000.0,
    booker_contracts=1,
    scale_out_contracts=2,
)


def F(contracts: int, value: float, reason: str) -> Fill:
    """A fill compared with its value to within float rounding."""
    return Fill(contracts, pytest.approx(value), reason)  # type: ignore[arg-type]


def booker(ex: ExitSettings = EX) -> OptionExit:
    return OptionExit("booker", 100.0, ex, point_value=20.0, cost=170.0)


def scale(ex: ExitSettings = EX) -> OptionExit:
    return OptionExit("scale_out", 100.0, ex, point_value=20.0, cost=170.0)


def test_config_exits_come_from_risk_yaml(research: ResearchSettings) -> None:
    e = research.exits
    assert e.stop_dollars == 750  # risk.stop_loss
    assert e.lock_start_dollars == 1000  # risk.profit_target
    assert e.lock_fraction == pytest.approx(0.5)  # risk.trail_keep_pct / 100
    assert e.first_target_dollars == 1000  # risk.profit_target
    assert (e.booker_contracts, e.scale_out_contracts) == (1, 2)


def test_contracts_per_kind() -> None:
    assert booker().contracts == 1
    assert scale().contracts == 2
    assert booker(replace(EX, booker_contracts=3)).contracts == 3
    with pytest.raises(ValueError, match="kind"):
        OptionExit("other", 100.0, EX, point_value=20.0, cost=170.0)


def test_stop_fills_at_its_level_inside_a_move_and_at_the_price_on_a_gap() -> None:
    x = booker()
    assert x.check(62.6, gap=False) == []
    assert x.open == 1
    assert x.check(62.0, gap=False) == [F(1, (62.5), "stop")]
    assert x.open == 0
    assert x.done
    assert booker().check(62.5, gap=False) == [F(1, (62.5), "stop")]
    assert booker().check(50.0, gap=True) == [Fill(1, 50.0, "stop")]


def test_lock_is_off_until_the_net_peak_reaches_the_start() -> None:
    x = booker()
    assert x.check(158.4, gap=False) == []  # net 998 < 1,000
    assert x.check(100.0, gap=False) == []  # no floor yet
    assert x.check(158.5, gap=False) == []  # net exactly 1,000: the lock starts
    assert x.check(133.6, gap=False) == []  # floor: net 500 -> value 133.5
    assert x.check(133.0, gap=False) == [F(1, (133.5), "lock")]


def test_lock_follows_the_peak_and_never_falls() -> None:
    x = booker()
    assert x.check(200.0, gap=False) == []  # net peak 1,830 -> floor net 915 -> 154.25
    assert x.check(180.0, gap=False) == []  # lower: the floor does not move
    assert x.check(154.3, gap=False) == []
    assert x.check(154.0, gap=False) == [F(1, (154.25), "lock")]


def test_lock_on_a_gap_fills_at_the_price() -> None:
    x = booker()
    x.check(200.0, gap=False)
    assert x.check(120.0, gap=True) == [Fill(1, 120.0, "lock")]


def test_lock_fraction_and_start_from_settings() -> None:
    x = booker(replace(EX, lock_fraction=0.25, lock_start_dollars=500.0))
    assert x.check(133.5, gap=False) == []  # net 500: starts; floor net 125 -> value 114.75
    assert x.check(114.8, gap=False) == []
    assert x.check(114.0, gap=False) == [F(1, (114.75), "lock")]


def test_stop_dollars_from_settings() -> None:
    x = booker(replace(EX, stop_dollars=200.0))  # value 90
    assert x.check(90.1, gap=False) == []
    assert x.check(80.0, gap=False) == [F(1, (90.0), "stop")]


def test_futures_stop_level_competes_and_the_first_reached_wins() -> None:
    # Falling: the higher level is reached first.
    assert booker().check(60.0, gap=False, futures_level=70.0) == [F(1, (70.0), "futures_stop")]
    assert booker().check(60.0, gap=False, futures_level=61.0) == [F(1, (62.5), "stop")]
    assert booker().check(80.0, gap=False, futures_level=80.0) == [F(1, (80.0), "futures_stop")]
    assert booker().check(55.0, gap=True, futures_level=70.0) == [Fill(1, 55.0, "futures_stop")]


def test_lock_and_futures_stop_together() -> None:
    x = booker()
    x.check(200.0, gap=False)  # floor 154.25
    assert x.check(150.0, gap=False, futures_level=152.0) == [F(1, (154.25), "lock")]


def test_scale_out_sells_one_at_the_target_and_the_other_rides_the_lock() -> None:
    x = scale()
    assert x.check(158.4, gap=False) == []
    assert x.check(160.0, gap=False) == [F(1, (158.5), "target")]
    assert x.open == 1
    assert not x.done
    assert x.check(140.0, gap=False) == []  # floor: peak net (160) 1,030 -> 515 -> 134.25
    assert x.check(130.0, gap=False) == [F(1, (134.25), "lock")]
    assert x.done


def test_scale_out_target_reached_exactly_fills() -> None:
    x = scale()
    assert x.check(158.5, gap=False) == [F(1, 158.5, "target")]  # net exactly +$1,000


def test_scale_out_target_on_a_gap_fills_at_the_price() -> None:
    x = scale()
    assert x.check(170.0, gap=True) == [Fill(1, 170.0, "target")]


def test_scale_out_stops_both_before_the_target() -> None:
    assert scale().check(60.0, gap=False) == [F(2, (62.5), "stop")]


def test_scale_out_target_from_settings() -> None:
    x = scale(replace(EX, first_target_dollars=330.0))  # value 125
    assert x.check(124.9, gap=False) == []
    assert x.check(126.0, gap=False) == [F(1, (125.0), "target")]


def test_scale_out_with_three_contracts_sells_one_and_rides_two() -> None:
    x = scale(replace(EX, scale_out_contracts=3))
    assert x.check(160.0, gap=False) == [F(1, (158.5), "target")]
    assert x.open == 2
    assert x.check(100.0, gap=False) == [F(2, (134.25), "lock")]


def test_flat_closes_what_is_open_at_the_price() -> None:
    x = scale()
    x.check(160.0, gap=False)
    assert x.flat(150.0, "flat") == [Fill(1, 150.0, "flat")]
    assert x.done
    assert x.flat(140.0, "flat") == []
    assert x.check(10.0, gap=False) == []


def test_nothing_is_ever_sold_twice() -> None:
    x = booker()
    x.check(60.0, gap=False)
    assert x.check(10.0, gap=False) == []
    assert x.flat(10.0, "flat") == []


def test_net_dollars_of_fills() -> None:
    x = scale()
    fills = x.check(160.0, gap=False) + x.check(130.0, gap=False)
    assert x.net(fills) == pytest.approx((158.5 - 100) * 20 - 170 + (134.25 - 100) * 20 - 170)


def test_marked_open_profit_counts_sold_and_open_contracts() -> None:
    """37-M3: the mark at a value = net of the contracts already sold + the open ones as
    if sold at that value, each round trip cost included."""
    x = scale()
    assert x.marked(100.0) == pytest.approx(-2 * 170)
    assert x.marked(90.0) == pytest.approx(2 * (-10 * 20 - 170))
    x.check(160.0, gap=False)  # one sold at 158.5
    sold = (158.5 - 100) * 20 - 170
    assert x.marked(150.0) == pytest.approx(sold + (50 * 20 - 170))
    x.check(130.0, gap=False)  # the other at 134.25
    final = sold + (134.25 - 100) * 20 - 170
    assert x.marked(10.0) == pytest.approx(final)
