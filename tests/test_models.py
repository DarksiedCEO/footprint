from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from footprint.core.models import (
    Bar,
    Direction,
    Read,
    Score,
    Signal,
    Trade,
    TradeEvent,
    TradeState,
)

TS = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def make_bar(**overrides: Any) -> Bar:
    values: dict[str, Any] = {
        "symbol": "TEST",
        "ts": TS,
        "open": Decimal("10"),
        "high": Decimal("12"),
        "low": Decimal("9"),
        "close": Decimal("11"),
        "volume": 5,
    }
    return Bar(**(values | overrides))


def make_read(**overrides: Any) -> Read:
    values: dict[str, Any] = {
        "agent_id": "agent-a",
        "ts": TS,
        "score": Score.BULLISH,
        "reason": "because",
    }
    return Read(**(values | overrides))


class TestBar:
    def test_valid(self) -> None:
        assert make_bar().close == Decimal("11")

    def test_flat_bar_is_valid(self) -> None:
        flat = Decimal("10")
        make_bar(open=flat, high=flat, low=flat, close=flat, volume=0)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"symbol": ""},
            {"symbol": "  "},
            {"ts": datetime(2026, 1, 5, 14, 30)},  # noqa: DTZ001 - naive on purpose
            {"ts": datetime(2026, 1, 5, 14, 30, tzinfo=timezone(timedelta(hours=-8)))},
            {"high": Decimal("8")},  # below low
            {"high": Decimal("10.5")},  # below close
            {"low": Decimal("10.5")},  # above open
            {"open": Decimal("13")},  # above high
            {"close": Decimal("8")},  # below low
            {"volume": -1},
            {"close": Decimal("NaN")},
            {"high": Decimal("Infinity")},
        ],
    )
    def test_rejects_invalid_values(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="Bar"):
            make_bar(**overrides)

    @pytest.mark.parametrize(
        "overrides",
        [{"open": 10.0}, {"volume": 1.5}, {"volume": True}, {"ts": "2026-01-05"}],
    )
    def test_rejects_wrong_types(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(TypeError, match="Bar"):
            make_bar(**overrides)

    def test_is_frozen(self) -> None:
        bar = make_bar()
        with pytest.raises(FrozenInstanceError):
            bar.close = Decimal("1")  # type: ignore[misc]


class TestRead:
    @pytest.mark.parametrize("score", list(Score))
    def test_accepts_each_score(self, score: Score) -> None:
        assert make_read(score=score).score is score

    def test_scores_are_exactly_minus_one_zero_plus_one(self) -> None:
        assert {int(score) for score in Score} == {-1, 0, 1}

    @pytest.mark.parametrize("score", [2, -2, 1, 0.5, "1", None, True])
    def test_rejects_anything_that_is_not_a_score(self, score: object) -> None:
        with pytest.raises(ValueError, match=r"Read\.score"):
            make_read(score=score)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"agent_id": ""},
            {"reason": ""},
            {"reason": "   "},
            {"ts": datetime(2026, 1, 5, 14, 30)},  # noqa: DTZ001 - naive on purpose
        ],
    )
    def test_rejects_invalid_values(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="Read"):
            make_read(**overrides)

    def test_has_no_order_fields(self) -> None:
        assert [f.name for f in fields(Read)] == ["agent_id", "ts", "score", "reason"]

    def test_cannot_grow_order_fields_at_runtime(self) -> None:
        read = make_read()
        assert not hasattr(read, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            read.order = "BUY 1"  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            make_read(quantity=1)


class TestSignal:
    def test_valid(self) -> None:
        signal = Signal(ts=TS, direction=Direction.LONG, reads=(make_read(),))
        assert signal.direction is Direction.LONG

    @pytest.mark.parametrize("direction", ["LONG", "FLAT", None])
    def test_rejects_invalid_direction(self, direction: object) -> None:
        with pytest.raises(ValueError, match=r"Signal\.direction"):
            Signal(ts=TS, direction=direction, reads=(make_read(),))  # type: ignore[arg-type]

    def test_rejects_no_reads(self) -> None:
        with pytest.raises(ValueError, match=r"Signal\.reads"):
            Signal(ts=TS, direction=Direction.SHORT, reads=())

    @pytest.mark.parametrize("reads", [[make_read()], ("not a read",)])
    def test_rejects_wrong_read_types(self, reads: object) -> None:
        with pytest.raises(TypeError, match=r"Signal\.reads"):
            Signal(ts=TS, direction=Direction.SHORT, reads=reads)  # type: ignore[arg-type]

    def test_rejects_naive_ts(self) -> None:
        with pytest.raises(ValueError, match=r"Signal\.ts"):
            Signal(
                ts=datetime(2026, 1, 5),  # noqa: DTZ001 - naive on purpose
                direction=Direction.LONG,
                reads=(make_read(),),
            )


class TestTrade:
    def test_states_are_the_agreed_lifecycle_in_order(self) -> None:
        assert [state.value for state in TradeState] == [
            "SCANNING",
            "SIGNAL",
            "ENTERING",
            "OPEN",
            "RUNNING",
            "EXITING",
            "CLOSED",
        ]

    def test_valid_with_ordered_events(self) -> None:
        events = (
            TradeEvent(ts=TS, state=TradeState.SCANNING),
            TradeEvent(ts=TS + timedelta(minutes=1), state=TradeState.SIGNAL, note="seen"),
        )
        trade = Trade(id="t-1", state=TradeState.SIGNAL, events=events)
        assert trade.events == events

    def test_valid_without_events(self) -> None:
        assert Trade(id="t-1", state=TradeState.SCANNING).events == ()

    def test_rejects_out_of_order_events(self) -> None:
        events = (
            TradeEvent(ts=TS, state=TradeState.SCANNING),
            TradeEvent(ts=TS - timedelta(seconds=1), state=TradeState.SIGNAL),
        )
        with pytest.raises(ValueError, match="chronological"):
            Trade(id="t-1", state=TradeState.SIGNAL, events=events)

    def test_rejects_state_that_disagrees_with_log(self) -> None:
        events = (TradeEvent(ts=TS, state=TradeState.SCANNING),)
        with pytest.raises(ValueError, match="last event"):
            Trade(id="t-1", state=TradeState.OPEN, events=events)

    def test_rejects_invalid_state_and_id(self) -> None:
        with pytest.raises(ValueError, match=r"Trade\.state"):
            Trade(id="t-1", state="PENDING")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=r"Trade\.id"):
            Trade(id="", state=TradeState.SCANNING)

    def test_rejects_mutable_event_log(self) -> None:
        with pytest.raises(TypeError, match=r"Trade\.events"):
            Trade(id="t-1", state=TradeState.SCANNING, events=[])  # type: ignore[arg-type]
