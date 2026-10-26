"""The alarm line: delivers alarms to Andre off the trading thread.

A pager, a phone push or an email can be slow or down. The trading loop must never
wait on one: `submit` only queues (it never blocks on the sink and never raises), and
a worker thread does the delivering.

- Order is kept: alarms are delivered in the order they were submitted.
- While the sink fails, the worker retries the same alarm every `retry` and does
  not move on (so nothing is skipped or reordered).
- An alarm whose message is already waiting is not queued twice.
- At most `max_pending` alarms wait; beyond that the oldest waiting one is dropped
  and counted, and once the line works again Andre is told how many were dropped.
- `close` stops the worker after it has tried to deliver what is waiting.
- A pager can hang (never answer, never fail). If the primary sink has been stuck
  on one alarm for longer than `retry`, or its last try failed, every alarm (and
  the backlog, once) is also handed to the `fallback` line, a second channel with
  its own worker, so a hung pager cannot silence the system.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta

from footprint.core.config import AlarmSettings
from footprint.execution.management.malik import Alarm

log = logging.getLogger("footprint.alarms")


class AlarmLine:
    def __init__(
        self,
        sink: Callable[[Alarm], None],
        *,
        retry: timedelta,
        max_pending: int,
        fallback: AlarmLine | None = None,
    ) -> None:
        if retry <= timedelta(0):
            raise ValueError("retry must be positive")
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._sink = sink
        self._retry = retry.total_seconds()
        self._max_pending = max_pending
        self._pending: deque[Alarm] = deque()
        self._dropped = 0
        self._last_dropped: Alarm | None = None
        self._delivered = 0
        self._closing = False
        self._in_flight: Alarm | None = None
        self._in_flight_since: float | None = None  # time.monotonic()
        self._failing = False
        self._fallback = fallback
        self._forwarding = False
        self._cv = threading.Condition()
        self._worker = threading.Thread(target=self._run, name="alarm-line", daemon=True)
        self._worker.start()
        self._watchdog: threading.Thread | None = None
        if fallback is not None:
            self._watchdog = threading.Thread(target=self._watch, name="alarm-watch", daemon=True)
            self._watchdog.start()

    @classmethod
    def from_settings(
        cls,
        sink: Callable[[Alarm], None],
        settings: AlarmSettings,
        fallback: AlarmLine | None = None,
    ) -> AlarmLine:
        """The live line, with the retry and cap from config/execution.yaml."""
        return cls(
            sink,
            retry=timedelta(seconds=settings.retry_seconds),
            max_pending=settings.max_pending,
            fallback=fallback,
        )

    def _unhealthy(self) -> bool:
        """Under the lock: the last try failed, or one delivery is taking too long."""
        if self._failing:
            return True
        since = self._in_flight_since
        return since is not None and time.monotonic() - since > self._retry

    def submit(self, alarm: Alarm) -> None:
        """Queue an alarm. Never blocks on the sink and never raises."""
        try:
            log.critical(alarm.message)
            forward: list[Alarm] = []
            with self._cv:
                if self._fallback is not None and self._unhealthy():
                    forward = self._take_backlog()
                    forward.append(alarm)
                if self._closing:
                    log.critical("alarm line closed; not delivered: %s", alarm.message)
                elif not any(waiting.message == alarm.message for waiting in self._pending):
                    if len(self._pending) >= self._max_pending:
                        # Drop the oldest one that is not being delivered right now.
                        victim = next(
                            (w for w in self._pending if w is not self._in_flight),
                            self._pending[0],
                        )
                        self._pending.remove(victim)
                        self._last_dropped = victim
                        self._dropped += 1
                    self._pending.append(alarm)
                    self._cv.notify_all()
            self._hand_over(forward)
        except Exception:  # the trading loop must never be hurt by its alarm line
            log.exception("could not queue an alarm")

    def _take_backlog(self) -> list[Alarm]:
        """Under the lock: the waiting alarms, the first time the primary is found
        unhealthy in this spell (later alarms are forwarded one by one)."""
        if self._forwarding:
            return []
        self._forwarding = True
        return list(self._pending)

    def _hand_over(self, alarms: list[Alarm]) -> None:
        """Outside the lock: the fallback line has its own."""
        if self._fallback is None:
            return
        for alarm in alarms:
            self._fallback.submit(alarm)

    def _watch(self) -> None:
        """With a fallback: notice a hung or failing primary even when no new alarm
        arrives, and hand the backlog over."""
        while True:
            with self._cv:
                if self._closing:
                    return
                self._cv.wait(self._retry)
                if self._closing:
                    return
                backlog = self._take_backlog() if self._unhealthy() else []
            self._hand_over(backlog)

    __call__ = submit  # an AlarmLine is the runner's `on_alarm`

    @property
    def pending(self) -> int:
        with self._cv:
            return len(self._pending)

    @property
    def delivered(self) -> int:
        with self._cv:
            return self._delivered

    @property
    def dropped(self) -> int:
        with self._cv:
            return self._dropped

    def close(self, timeout: timedelta) -> bool:
        """Stop after trying what is waiting. True if nothing was left undelivered."""
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        self._worker.join(timeout.total_seconds())
        if self._watchdog is not None:
            self._watchdog.join(timeout.total_seconds())
        with self._cv:
            left = list(self._pending)
        for alarm in left:
            log.critical("alarm line closed with this undelivered: %s", alarm.message)
        return not left and not self._worker.is_alive()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._closing:
                    self._cv.wait()
                if not self._pending:
                    return  # closing, and nothing left
                alarm = self._pending[0]
                self._in_flight = alarm
                self._in_flight_since = time.monotonic()
            try:
                self._sink(alarm)
            except Exception:
                log.exception("alarm line down; retrying %s", alarm.message)
                with self._cv:
                    self._in_flight, self._in_flight_since = None, None
                    self._failing = True
                    # Paced: new alarms arriving do not cut the wait short.
                    deadline = time.monotonic() + self._retry
                    while not self._closing:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        self._cv.wait(left)
                    if self._closing:
                        return  # one last try was made; close reports what is left
                continue
            with self._cv:
                self._in_flight, self._in_flight_since = None, None
                self._failing = False
                if self._pending and self._pending[0] is alarm:
                    self._pending.popleft()
                if not self._pending:
                    # Caught up: a later outage hands its backlog over again. (Not on
                    # every success, or a slow-but-working pager would hand the same
                    # backlog to the fallback once per delivery.)
                    self._forwarding = False
                self._delivered += 1
                if self._dropped and self._last_dropped is not None:
                    note = Alarm(
                        self._last_dropped.ts,
                        f"{self._dropped} alarm(s) were dropped while the alarm line was "
                        "down; see the log for all of them",
                    )
                    self._dropped, self._last_dropped = 0, None
                    self._pending.appendleft(note)
