"""Research rules, by name. Each entry builds a rule from the research settings.

A rule implements `footprint.research.engine.Rule`: asked with a session day and the
index of the bar that has just closed, it may look at bars up to that index only.
"""

from __future__ import annotations

from collections.abc import Callable

from footprint.research.engine import Rule
from footprint.research.settings import ResearchSettings

RULES: dict[str, Callable[[ResearchSettings], Rule]] = {}
