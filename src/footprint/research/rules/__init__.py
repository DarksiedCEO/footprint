"""Research rules, by name.

A rule implements `footprint.research.engine.Rule`: asked with a session day and the
index of the bar that has just closed, it may look at bars up to that index only.
A registered rule lists its pre-registered variants (e.g. a green-stop grid) and builds
one of them; a rule without variants lists none and is built with None.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from footprint.research.engine import Rule
from footprint.research.rules.london_orb import build_orb, orb_variants
from footprint.research.settings import ResearchSettings


@dataclass(frozen=True, slots=True)
class Registered:
    variants: Callable[[ResearchSettings], tuple[str, ...]]
    build: Callable[[ResearchSettings, str | None], Rule]


RULES: dict[str, Registered] = {
    "london_orb": Registered(orb_variants, build_orb),
}
