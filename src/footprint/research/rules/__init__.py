"""Research rules, by name.

A rule implements `footprint.research.engine.Rule`: asked with a session day and the
index of the bar that has just closed, it may look at bars up to that index only.
A registered rule lists its pre-registered variants (e.g. a green-stop grid) and builds
one of them; a rule without variants lists none and is built with None.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from footprint.core.config import ConfigError
from footprint.research.engine import Rule
from footprint.research.rules.london_orb import build_orb, orb_variants
from footprint.research.rules.noise_band import build_noise, noise_variants
from footprint.research.settings import ResearchSettings


@dataclass(frozen=True, slots=True)
class Registered:
    variants: Callable[[ResearchSettings], tuple[str, ...]]
    build: Callable[[ResearchSettings, str | None], Rule]
    t_bar_required: bool = False  # pre-registered with a t bar: never run without one


RULES: dict[str, Registered] = {
    "london_orb": Registered(orb_variants, build_orb),
    "noise_band": Registered(noise_variants, build_noise, t_bar_required=True),
}


def check_t_bars(settings: ResearchSettings, rule: str) -> None:
    """Fail closed on the t bars (review M-1): every name in judging.min_t_stat must be
    a registered rule, and a rule pre-registered with a t bar must have one."""
    where = "research.yaml: judging: min_t_stat"
    unknown = sorted(set(settings.judging.min_t_stat) - set(RULES))
    if unknown:
        raise ConfigError(f"{where}: {unknown} are not registered rules ({sorted(RULES)})")
    if RULES[rule].t_bar_required and rule not in settings.judging.min_t_stat:
        raise ConfigError(f"{where}: {rule} was pre-registered with a t bar; none is set")
