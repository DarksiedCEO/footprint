"""PR9: alarms are delivered off the trading thread, in order, retried, bounded."""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.alarms import AlarmLine
from footprint.execution.management.malik import Alarm
from tests.execution.conftest import Stack

T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
FAST = timedelta(milliseconds=10)
SEC_ = timedelta(seconds=1)
WAIT = timedelta(seconds=5)  # generous upper bound for the worker in a slow CI


def alarm(n: int) -> Alarm:
    return Alarm(T + timedelta(seconds=n), f"alarm {n}")


def eventually(condition: object, timeout: float = WAIT.total_seconds()) -> bool:
    assert callable(condition)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


def test_submit_never_waits_on_a_slow_pager() -> None:
    release = threading.Event()
    heard: list[str] = []

    def slow(a: Alarm) -> None:
        release.wait(WAIT.total_seconds())
        heard.append(a.message)

    line = AlarmLine(slow, retry=FAST, max_pending=10)
    started = time.monotonic()
    for n in range(5):
        line.submit(alarm(n))
    assert time.monotonic() - started < 0.5  # the pager is blocked; submit is not
    release.set()
    assert eventually(lambda: len(heard) == 5)
    assert heard == [f"alarm {n}" for n in range(5)]  # in order
    assert line.close(WAIT)


def test_a_down_pager_is_retried_without_skipping_or_reordering() -> None:
    heard: list[str] = []
    failures = {"left": 3}

    def flaky(a: Alarm) -> None:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise ConnectionError("pager down")
        heard.append(a.message)

    line = AlarmLine(flaky, retry=FAST, max_pending=10)
    for n in range(3):
        line.submit(alarm(n))
    assert eventually(lambda: len(heard) == 3)
    assert heard == ["alarm 0", "alarm 1", "alarm 2"]
    assert line.close(WAIT)


def test_a_waiting_message_is_not_queued_twice() -> None:
    gate = threading.Event()
    heard: list[str] = []

    def held(a: Alarm) -> None:
        gate.wait(WAIT.total_seconds())
        heard.append(a.message)

    line = AlarmLine(held, retry=FAST, max_pending=10)
    line.submit(alarm(0))  # being delivered (held)
    for _ in range(5):
        line.submit(alarm(1))  # the same message, again and again
    assert line.pending <= 2
    gate.set()
    assert eventually(lambda: len(heard) == 2)
    assert heard == ["alarm 0", "alarm 1"]
    assert line.close(WAIT)


def test_beyond_the_cap_the_oldest_are_dropped_and_counted() -> None:
    # Deterministic (G44-2): the first delivery of "alarm 0" is held inside the pager
    # until all ten alarms are submitted, so the cap is always hit with alarm 0 in
    # flight. The outcome no longer depends on when the worker thread is scheduled.
    entered = threading.Event()  # set = alarm 0 is inside the pager
    submitted = threading.Event()  # set = all ten alarms are submitted
    down = threading.Event()  # set = the line works
    heard: list[str] = []

    def pager(a: Alarm) -> None:
        if not entered.is_set():
            entered.set()
            submitted.wait(WAIT.total_seconds())
            raise ConnectionError("pager down")
        if not down.is_set():
            raise ConnectionError("pager down")
        heard.append(a.message)

    line = AlarmLine(pager, retry=FAST, max_pending=3)
    line.submit(alarm(0))
    assert entered.wait(WAIT.total_seconds())
    for n in range(1, 10):
        line.submit(alarm(n))
    assert line.pending == 3
    assert line.dropped == 7
    submitted.set()
    down.set()
    assert eventually(lambda: line.pending == 0 and len(heard) == 4)
    # The alarm in flight when the cap is hit is never dropped; of the rest the oldest
    # go (alarms 1 to 7) and the newest survive, in order.
    assert heard[0] == "alarm 0"
    assert heard[1].startswith("7 alarm(s) were dropped")  # told right after it is back
    assert heard[2:] == ["alarm 8", "alarm 9"]
    assert line.close(WAIT)


def test_the_cap_never_drops_the_alarm_being_delivered() -> None:
    gate = threading.Event()
    heard: list[str] = []

    def held(a: Alarm) -> None:
        if a.message == "alarm 0":
            gate.wait(WAIT.total_seconds())
        heard.append(a.message)

    line = AlarmLine(held, retry=FAST, max_pending=2)
    line.submit(alarm(0))
    assert eventually(lambda: line._in_flight is not None)  # alarm 0 is mid-delivery
    line.submit(alarm(1))
    line.submit(alarm(2))  # over the cap: alarm 1 goes, not the one in flight
    gate.set()
    assert eventually(lambda: line.pending == 0 and "alarm 2" in heard)
    assert heard == [
        "alarm 0",
        "1 alarm(s) were dropped while the alarm line was down; see the log for all of them",
        "alarm 2",
    ]
    assert line.close(WAIT)


def test_a_cap_of_one_never_loses_the_newest() -> None:
    gate = threading.Event()
    heard: list[str] = []

    def held(a: Alarm) -> None:
        if a.message == "alarm 0":
            gate.wait(WAIT.total_seconds())
        heard.append(a.message)

    line = AlarmLine(held, retry=FAST, max_pending=1)
    line.submit(alarm(0))
    assert eventually(lambda: line._in_flight is not None)
    line.submit(alarm(1))  # the only one waiting is in flight: it is dropped from the queue
    gate.set()
    assert eventually(lambda: "alarm 1" in heard)  # the newest is still delivered
    assert line.close(WAIT)


def test_an_alarm_after_close_is_logged_not_queued() -> None:
    heard: list[str] = []
    line = AlarmLine(lambda a: heard.append(a.message), retry=FAST, max_pending=3)
    assert line.close(WAIT)
    line.submit(alarm(0))
    assert line.pending == 0
    assert heard == []


def test_a_broken_submit_path_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    line = AlarmLine(lambda a: None, retry=FAST, max_pending=3)

    class Exploding:
        message = property(lambda self: (_ for _ in ()).throw(RuntimeError("bad")))

    line.submit(Exploding())  # type: ignore[arg-type]  # must not raise
    assert line.close(WAIT)


def test_close_reports_what_could_not_be_delivered() -> None:
    def down(a: Alarm) -> None:
        raise ConnectionError("pager down")

    line = AlarmLine(down, retry=timedelta(seconds=60), max_pending=3)
    line.submit(alarm(0))
    assert not line.close(WAIT)  # one last try failed: it says so
    line.submit(alarm(1))  # after close: logged, never raises


def test_bad_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="retry"):
        AlarmLine(lambda a: None, retry=timedelta(0), max_pending=3)
    with pytest.raises(ValueError, match="max_pending"):
        AlarmLine(lambda a: None, retry=FAST, max_pending=0)


def test_the_runner_never_waits_on_a_hung_pager(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """End to end: the runner's alarm line is an AlarmLine whose pager hangs. Every
    step still finishes promptly, and the alarms arrive once the pager answers."""
    from tests.execution.test_runner import open_trade, runner_for

    hung = threading.Event()
    heard: list[str] = []

    def pager(a: Alarm) -> None:
        hung.wait(WAIT.total_seconds())
        heard.append(a.message)

    runner, _, _ = runner_for(stack, bars, tmp_path)
    line = AlarmLine(pager, retry=FAST, max_pending=100)
    runner._on_alarm = line
    fill = open_trade(stack, runner)
    stack.ledger.latch_kill_switch("test")  # management alarms as it flattens
    started = time.monotonic()
    for second in range(1, 6):
        stack.quote(150, 152, fill + second * SEC_)
        runner.step(fill + second * SEC_)
    assert time.monotonic() - started < 2.0  # the pager is hung; the loop is not
    assert stack.broker.positions() == {}  # the flatten happened meanwhile
    hung.set()
    assert eventually(lambda: any("KILL SWITCH" in m for m in heard))
    assert line.close(WAIT)


def test_the_live_line_reads_its_settings_from_config(stack: Stack) -> None:
    settings = stack.profiles.execution.alarms
    line = AlarmLine.from_settings(lambda a: None, settings)
    assert line._retry == settings.retry_seconds
    assert line._max_pending == settings.max_pending
    assert line.close(WAIT)


def test_retries_are_paced_even_while_new_alarms_arrive() -> None:
    """A down pager is retried every `retry`, not once per new alarm."""
    tries: list[float] = []

    def down(a: Alarm) -> None:
        tries.append(time.monotonic())
        raise ConnectionError("pager down")

    line = AlarmLine(down, retry=timedelta(milliseconds=200), max_pending=100)
    started = time.monotonic()
    for n in range(40):
        line.submit(alarm(n))
        time.sleep(0.01)
    elapsed = time.monotonic() - started
    assert len(tries) <= elapsed / 0.2 + 2  # paced by retry, not by submits
    line.close(WAIT)


def test_a_hung_pager_hands_the_backlog_to_the_fallback() -> None:
    """The primary never answers and never fails. Without any new alarm, the backlog
    reaches the fallback channel; later alarms go there too."""
    hung = threading.Event()
    backup: list[str] = []

    def pager(a: Alarm) -> None:
        hung.wait(WAIT.total_seconds())

    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=10)
    line = AlarmLine(pager, retry=timedelta(milliseconds=50), max_pending=10, fallback=fallback)
    line.submit(alarm(0))
    line.submit(alarm(1))
    assert eventually(lambda: backup == ["alarm 0", "alarm 1"])  # the watchdog did it
    line.submit(alarm(2))
    assert eventually(lambda: backup == ["alarm 0", "alarm 1", "alarm 2"])
    hung.set()
    line.close(WAIT)
    assert fallback.close(WAIT)


def test_a_failing_pager_also_uses_the_fallback() -> None:
    backup: list[str] = []

    def down(a: Alarm) -> None:
        raise ConnectionError("pager down")

    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=10)
    line = AlarmLine(down, retry=timedelta(milliseconds=50), max_pending=10, fallback=fallback)
    line.submit(alarm(0))
    assert eventually(lambda: "alarm 0" in backup)
    line.submit(alarm(1))
    assert eventually(lambda: "alarm 1" in backup)
    line.close(WAIT)
    assert fallback.close(WAIT)


def test_a_healthy_pager_never_uses_the_fallback() -> None:
    backup: list[str] = []
    heard: list[str] = []
    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=10)
    line = AlarmLine(
        lambda a: heard.append(a.message),
        retry=timedelta(milliseconds=50),
        max_pending=10,
        fallback=fallback,
    )
    for n in range(5):
        line.submit(alarm(n))
    assert eventually(lambda: len(heard) == 5)
    time.sleep(0.2)  # several watchdog rounds
    assert backup == []
    assert line.close(WAIT)
    assert fallback.close(WAIT)


def test_a_second_outage_hands_its_backlog_over_again() -> None:
    backup: list[str] = []
    state = {"down": True}
    heard: list[str] = []

    def pager(a: Alarm) -> None:
        if state["down"]:
            raise ConnectionError("pager down")
        heard.append(a.message)

    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=10)
    line = AlarmLine(pager, retry=timedelta(milliseconds=50), max_pending=10, fallback=fallback)
    line.submit(alarm(0))
    assert eventually(lambda: "alarm 0" in backup)  # first outage
    state["down"] = False
    assert eventually(lambda: "alarm 0" in heard)  # recovered
    state["down"] = True
    line.submit(alarm(1))  # fails on the primary
    line.submit(alarm(2))
    assert eventually(lambda: "alarm 1" in backup and "alarm 2" in backup)  # second outage
    line.close(WAIT)
    assert fallback.close(WAIT)


def test_a_slow_but_working_pager_hands_the_backlog_over_at_most_once() -> None:
    backup: list[str] = []
    heard: list[str] = []

    def slow(a: Alarm) -> None:
        time.sleep(0.08)  # each delivery takes longer than retry: "unhealthy" while busy
        heard.append(a.message)

    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=20)
    line = AlarmLine(slow, retry=timedelta(milliseconds=30), max_pending=20, fallback=fallback)
    for n in range(5):
        line.submit(alarm(n))
    assert eventually(lambda: len(heard) == 5)
    assert len(backup) == len(set(backup))  # never the same alarm handed over twice
    assert line.close(WAIT)
    assert fallback.close(WAIT)


def test_an_alarm_queued_while_a_flapping_pager_looks_healthy_still_reaches_the_fallback() -> None:
    """Audit 2026-10-07 A-5: down, up for one delivery, down again. An alarm queued in
    the moment it looked healthy must still be handed to the fallback."""
    calls = [0]
    queued = threading.Event()

    def flapping(a: Alarm) -> None:
        calls[0] += 1
        if calls[0] == 2:  # only the second delivery attempt gets through
            return
        if calls[0] == 3:  # the next one fails only once alarm 3 is queued
            queued.wait(WAIT.total_seconds())
        raise ConnectionError("pager down")

    backup: list[str] = []
    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=10)
    line = AlarmLine(flapping, retry=timedelta(milliseconds=50), max_pending=10, fallback=fallback)
    line.submit(alarm(1))
    line.submit(alarm(2))
    assert eventually(lambda: line.delivered == 1)  # alarm 1, on its retry
    line.submit(alarm(3))  # the pager looks healthy right now
    queued.set()
    assert eventually(lambda: "alarm 3" in backup)
    line.close(timedelta(milliseconds=100))
    assert fallback.close(WAIT)


def test_an_alarm_raised_while_closing_with_a_failing_pager_still_reaches_the_fallback() -> None:
    """Delta review F-8: the closing line will not deliver it; the fallback must."""

    def down(a: Alarm) -> None:
        raise ConnectionError("pager down")

    backup: list[str] = []
    fallback = AlarmLine(lambda a: backup.append(a.message), retry=FAST, max_pending=10)
    line = AlarmLine(down, retry=timedelta(milliseconds=50), max_pending=10, fallback=fallback)
    line.submit(alarm(1))
    assert eventually(lambda: "alarm 1" in backup)  # the pager failed: handed over
    line.close(timedelta(milliseconds=100))
    line.submit(alarm(2))  # raised during shutdown
    assert eventually(lambda: "alarm 2" in backup)
    assert fallback.close(WAIT)
