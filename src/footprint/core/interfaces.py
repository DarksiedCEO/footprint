"""The seams between floors. Implementations live on their own floor."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

from footprint.core.models import Bar, Read


@runtime_checkable
class DataSource(Protocol):
    """Floor 1 seam: anything that can produce bars in chronological order."""

    def bars(self) -> Iterator[Bar]: ...


@runtime_checkable
class Reader(Protocol):
    """Floor 2 seam: an agent that turns bars into a single Read."""

    @property
    def agent_id(self) -> str: ...

    def read(self, bars: Sequence[Bar]) -> Read: ...


@runtime_checkable
class Broker(Protocol):
    """Floor 4 seam: connection lifecycle only.

    Order methods are deliberately absent from the skeleton; they get designed
    together with the execution floor.
    """

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool: ...
