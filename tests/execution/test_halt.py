"""Review N5/N6: the halt alarms once per reason, to one sink, and never throws."""

from footprint.execution.halt import Halt


def test_an_identical_halt_alarms_once() -> None:
    halt = Halt()
    heard: list[str] = []
    halt.route(heard.append)
    halt.trip("reconcile mismatch")
    halt.trip("reconcile mismatch")
    halt.trip("exit failed")
    assert heard == ["HALT: reconcile mismatch", "HALT: exit failed"]
    assert halt.reasons == ("reconcile mismatch", "exit failed")


def test_a_later_route_replaces_the_earlier_sink() -> None:
    halt = Halt()
    old: list[str] = []
    new: list[str] = []
    halt.route(old.append)
    halt.route(new.append)
    halt.trip("x")
    assert (old, new) == ([], ["HALT: x"])


def test_a_broken_sink_never_stops_the_halt() -> None:
    halt = Halt()

    def broken(message: str) -> None:
        raise ConnectionError("pager down")

    halt.route(broken)
    halt.trip("x")  # must not raise
    assert halt.tripped
