from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from footprint.core.models import (
    ALLOWED_TRANSITIONS,
    Bar,
    Direction,
    IllegalTransitionError,
    Read,
    Score,
    Signal,
    Trade,
    TradeEvent,
    TradeState,
)

TS = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

ALLOWED = [
    (TradeState.SCANNING, TradeState.SIGNAL),
    (TradeState.SIGNAL, TradeState.ENTERING),
    (TradeState.SIGNAL, TradeState.CLOSED),
    (TradeState.ENTERING, TradeState.OPEN),
    (TradeState.ENTERING, TradeState.CLOSED),
    (TradeState.OPEN, TradeState.RUNNING),
    (TradeState.OPEN, TradeState.EXITING),
    (TradeState.RUNNING, TradeState.EXITING),
    (TradeState.EXITING, TradeState.CLOSED),
]

LATER = TS + timedelta(minutes=1)

# The shortest legal route from SCANNING to each state.
ROUTES = {
    TradeState.SCANNING: [],
    TradeState.SIGNAL: [TradeState.SIGNAL],
    TradeState.ENTERING: [TradeState.SIGNAL, TradeState.ENTERING],
    TradeState.OPEN: [TradeState.SIGNAL, TradeState.ENTERING, TradeState.OPEN],
    TradeState.RUNNING: [
        TradeState.SIGNAL,
        TradeState.ENTERING,
        TradeState.OPEN,
        TradeState.RUNNING,
    ],
    TradeState.EXITING: [
        TradeState.SIGNAL,
        TradeState.ENTERING,
        TradeState.OPEN,
        TradeState.EXITING,
    ],
    TradeState.CLOSED: [TradeState.SIGNAL, TradeState.CLOSED],
}


def trade_in(state: TradeState) -> Trade:
    trade = Trade.start("t-1", TS)
    for step in ROUTES[state]:
        trade = trade.transition(step, TS)
    return trade


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
            {"low": Decimal("0"), "open": Decimal("0")},
            {"low": Decimal("-1")},
            {
                "open": Decimal("-10"),
                "high": Decimal("-8"),
                "low": Decimal("-11"),
                "close": Decimal("-9"),
            },
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

    @pytest.mark.parametrize(("current", "target"), ALLOWED)
    def test_allows_each_legal_transition(self, current: TradeState, target: TradeState) -> None:
        before = trade_in(current)
        moved = before.transition(target, LATER, note="why")
        assert moved.state is target
        assert moved.events == (*before.events, TradeEvent(ts=LATER, state=target, note="why"))
        assert moved.id == before.id

    def test_allowed_table_is_exactly_the_agreed_one(self) -> None:
        table = {(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
        assert table == set(ALLOWED)
        assert set(ALLOWED_TRANSITIONS) == set(TradeState)

    @pytest.mark.parametrize(
        ("current", "target"),
        [(a, b) for a in TradeState for b in TradeState if (a, b) not in ALLOWED],
    )
    def test_rejects_every_other_transition(self, current: TradeState, target: TradeState) -> None:
        with pytest.raises(IllegalTransitionError, match=f"{current.value} -> {target.value}"):
            trade_in(current).transition(target, LATER)

    @pytest.mark.parametrize("target", list(TradeState))
    def test_closed_is_terminal(self, target: TradeState) -> None:
        with pytest.raises(IllegalTransitionError):
            trade_in(TradeState.CLOSED).transition(target, LATER)

    def test_start_opens_the_log_with_a_scanning_event(self) -> None:
        trade = Trade.start("t-1", TS, note="watching")
        assert trade.state is TradeState.SCANNING
        assert trade.events == (TradeEvent(ts=TS, state=TradeState.SCANNING, note="watching"),)

    def test_transition_does_not_mutate_and_keeps_the_log(self) -> None:
        start = Trade.start("t-1", TS)
        later = start.transition(TradeState.SIGNAL, TS).transition(
            TradeState.CLOSED, LATER, note="vetoed"
        )
        assert len(start.events) == 1
        assert [event.state for event in later.events] == [
            TradeState.SCANNING,
            TradeState.SIGNAL,
            TradeState.CLOSED,
        ]

    def test_transition_rejects_going_back_in_time(self) -> None:
        with pytest.raises(ValueError, match="chronological"):
            Trade.start("t-1", TS).transition(TradeState.SIGNAL, TS - timedelta(seconds=1))

    def test_transition_rejects_non_state_target(self) -> None:
        with pytest.raises(TypeError, match="TradeState"):
            Trade.start("t-1", TS).transition("SIGNAL", TS)  # type: ignore[arg-type]

    def test_transition_from_an_empty_log_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"Trade\.start"):
            Trade(id="t-1", state=TradeState.SCANNING).transition(TradeState.SIGNAL, TS)

    @pytest.mark.parametrize("state", [s for s in TradeState if s is not TradeState.SCANNING])
    def test_rejects_empty_log_in_a_non_scanning_state(self, state: TradeState) -> None:
        with pytest.raises(ValueError, match="must start at SCANNING"):
            Trade(id="t-1", state=state)

    @pytest.mark.parametrize("state", [s for s in TradeState if s is not TradeState.SCANNING])
    def test_rejects_first_event_that_is_not_scanning(self, state: TradeState) -> None:
        with pytest.raises(ValueError, match="must start at SCANNING"):
            Trade(id="t-1", state=state, events=(TradeEvent(ts=TS, state=state),))

    def test_rejects_log_that_skips_scanning_even_if_otherwise_legal(self) -> None:
        events = (
            TradeEvent(ts=TS, state=TradeState.SIGNAL),
            TradeEvent(ts=LATER, state=TradeState.ENTERING),
        )
        with pytest.raises(ValueError, match="must start at SCANNING"):
            Trade(id="t-1", state=TradeState.ENTERING, events=events)

    @pytest.mark.parametrize(
        "ts",
        [
            datetime(2026, 1, 5, 14, 30),  # noqa: DTZ001 - naive on purpose
            datetime(2026, 1, 5, 14, 30, tzinfo=timezone(timedelta(hours=-8))),
        ],
    )
    def test_event_rejects_naive_or_non_utc_timestamp(self, ts: datetime) -> None:
        with pytest.raises(ValueError, match=r"TradeEvent\.ts"):
            TradeEvent(ts=ts, state=TradeState.SCANNING)
        with pytest.raises(ValueError, match=r"TradeEvent\.ts"):
            Trade.start("t-1", ts)
        with pytest.raises(ValueError, match=r"TradeEvent\.ts"):
            Trade.start("t-1", TS).transition(TradeState.SIGNAL, ts)

    def test_rejects_event_log_containing_an_illegal_jump(self) -> None:
        events = (
            TradeEvent(ts=TS, state=TradeState.SCANNING),
            TradeEvent(ts=TS + timedelta(minutes=1), state=TradeState.OPEN),
        )
        with pytest.raises(IllegalTransitionError, match="SCANNING -> OPEN"):
            Trade(id="t-1", state=TradeState.OPEN, events=events)

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
