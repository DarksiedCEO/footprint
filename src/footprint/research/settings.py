"""Research settings, loaded from config/research.yaml plus the profiles they reuse.

No number lives in code. Values that already exist elsewhere are read from there, never
copied: the exits from risk.yaml (stop_loss, profit_target, trail_keep_pct), the gates
and the strike step from backtest.yaml, the point value and symbol from the instrument
profile, the bankroll from risk.yaml.

Values that depend on the instrument (the reference price, the round trip cost, and an
instrument's own strike step where backtest.yaml's does not apply) live in research.yaml
`instruments`, keyed by the profile file the run uses. A profile not listed there is
refused: one instrument's cost is never used silently for another.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from footprint.core.config import (
    _PERCENT_MAX,
    ConfigError,
    _check_keys,
    _integer,
    _positive_decimal,
    _read_mapping,
    _text,
    load_instrument,
    load_risk,
    load_yaml,
)

OTHER = "other"  # the label of a minute that no configured session covers
_MINUTE = timedelta(minutes=1)
_DAY_MINUTES = timedelta(days=1) // _MINUTE
_TOP = {
    "timezone",
    "session_day",
    "sessions",
    "vol",
    "option",
    "instruments",
    "exits",
    "judging",
    "rules",
}


@dataclass(frozen=True, slots=True)
class Window:
    """A daily clock window [start, end); end before start wraps midnight."""

    start: time
    end: time

    def covers(self, clock: time) -> bool:
        if self.start < self.end:
            return self.start <= clock < self.end
        return clock >= self.start or clock < self.end


@dataclass(frozen=True, slots=True)
class GateSettings:
    """The go-live gates, read from config/backtest.yaml `gates`. Read here, not through
    footprint.backtest: nothing outside footprint.backtest may import it (the rules-seam
    guard, tests/backtest/test_rules_seam.py)."""

    min_trades: int
    min_win_rate_pct: Decimal
    min_payoff_ratio: Decimal
    min_profit_factor: Decimal
    max_drawdown_pct: Decimal
    out_of_sample_pct: Decimal
    min_out_of_sample_trades: int


def _backtest_parts(path: Path) -> tuple[GateSettings, Decimal]:
    """(gates, option_model.strike_step) from backtest.yaml, checked as its loader does."""
    where = str(path)
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read profile: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping")
    for key in ("gates", "option_model"):
        if key not in raw:
            raise ConfigError(f"{where}: missing keys: [{key!r}]")
    g_where = f"{where}: gates"
    g = _check_keys(raw["gates"], {f.name for f in fields(GateSettings)}, g_where)
    gates = GateSettings(
        min_trades=_integer(g, "min_trades", g_where, minimum=1),
        min_win_rate_pct=_positive_decimal(g, "min_win_rate_pct", g_where),
        min_payoff_ratio=_positive_decimal(g, "min_payoff_ratio", g_where),
        min_profit_factor=_positive_decimal(g, "min_profit_factor", g_where),
        max_drawdown_pct=_positive_decimal(g, "max_drawdown_pct", g_where),
        out_of_sample_pct=_positive_decimal(g, "out_of_sample_pct", g_where),
        min_out_of_sample_trades=_integer(g, "min_out_of_sample_trades", g_where, minimum=1),
    )
    for name in ("min_win_rate_pct", "max_drawdown_pct", "out_of_sample_pct"):
        if getattr(gates, name) >= _PERCENT_MAX:
            raise ConfigError(f"{g_where}: {name} must be below {_PERCENT_MAX}")
    o = raw["option_model"]
    if not isinstance(o, dict) or "strike_step" not in o:
        raise ConfigError(f"{where}: option_model: missing keys: ['strike_step']")
    step = _positive_decimal(o, "strike_step", f"{where}: option_model")
    return gates, step


@dataclass(frozen=True, slots=True)
class OptionSettings:
    moneyness_points: float  # strike offset in futures points; positive = in the money
    days_to_expiry: float  # calendar days to expiry at entry
    days_per_year: float
    rate: float
    reference_price: float | None  # None: no price-level rescaling
    strike_step: float
    point_value: float  # dollars per option point (the instrument's point value)


@dataclass(frozen=True, slots=True)
class AdrBracketSettings:
    """The `adr_bracket` exit: a futures stop and target, each a fraction of the rule's
    average daily range (the signal's `range_points`) from the entry price."""

    contracts: int
    stop_range_fraction: float
    target_range_fraction: float


@dataclass(frozen=True, slots=True)
class ExitSettings:
    stop_dollars: float  # per contract, gross option dollars
    lock_start_dollars: float  # per contract, net of the round-trip cost
    lock_fraction: float  # share of the peak net open profit locked
    first_target_dollars: float  # scale-out: per contract, net of the round-trip cost
    booker_contracts: int
    scale_out_contracts: int
    adr_bracket: AdrBracketSettings | None = None  # None: that exit cannot be used


@dataclass(frozen=True, slots=True)
class JudgingSettings:
    time_parts: int
    min_positive_parts: int
    # per rule name: the t statistic must also be at least this; a rule not listed has
    # no t bar
    min_t_stat: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    timezone: str
    roll: time
    sessions: tuple[tuple[str, Window], ...]
    vol_divisor: float
    vol_max_age_days: int
    option: OptionSettings
    round_trip_per_contract: float
    exits: ExitSettings
    judging: JudgingSettings
    gates: GateSettings
    bankroll: float
    symbol: str
    rules: Mapping[str, object]

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _clock(raw: Mapping[str, object], key: str, where: str) -> time:
    text = _text(raw, key, where)
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{where}: {key} must be HH:MM, got {text!r}") from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ConfigError(f"{where}: {key} must be HH:MM with no seconds, got {text!r}")
    return parsed


def _signed(raw: Mapping[str, object], key: str, where: str) -> float:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"{where}: {key} must be finite, got {value!r}")
    return number


def _sessions(raw: object, where: str) -> tuple[tuple[str, Window], ...]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{where}: sessions must be a non-empty mapping")
    windows: list[tuple[str, Window]] = []
    for name, body in raw.items():
        if not isinstance(name, str) or not name or name == OTHER:
            raise ConfigError(f"{where}: bad session name {name!r}")
        w_where = f"{where}: sessions: {name}"
        b = _check_keys(body, {"start", "end"}, w_where)
        window = Window(_clock(b, "start", w_where), _clock(b, "end", w_where))
        if window.start == window.end:
            raise ConfigError(f"{w_where}: start and end must differ")
        windows.append((name, window))
    for minute in range(_DAY_MINUTES):
        clock = (datetime.min.replace(tzinfo=UTC) + minute * _MINUTE).time()
        owners = [name for name, window in windows if window.covers(clock)]
        if len(owners) > 1:
            raise ConfigError(f"{where}: sessions {owners} overlap at {clock:%H:%M}")
    return tuple(windows)


def load_research(config_dir: Path, instrument_file: str) -> ResearchSettings:
    path = config_dir / "research.yaml"
    where = str(path)
    raw = _read_mapping(path, _TOP)
    timezone = _text(raw, "timezone", where)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{where}: unknown timezone {timezone!r}") from exc
    s_where = f"{where}: session_day"
    roll = _clock(_check_keys(raw["session_day"], {"roll"}, s_where), "roll", s_where)
    sessions = _sessions(raw["sessions"], where)

    v_where = f"{where}: vol"
    v = _check_keys(raw["vol"], {"divisor", "max_age_days"}, v_where)
    divisor = float(_positive_decimal(v, "divisor", v_where))
    max_age = _integer(v, "max_age_days", v_where, minimum=1)

    risk = load_risk(config_dir / "risk.yaml")
    gates, strike_step = _backtest_parts(config_dir / "backtest.yaml")
    instrument = load_instrument(config_dir / "instruments" / instrument_file)

    o_where = f"{where}: option"
    o = _check_keys(
        raw["option"], {"moneyness_points", "days_to_expiry", "days_per_year", "rate"}, o_where
    )
    i_where = f"{where}: instruments"
    listed = raw["instruments"]
    if not isinstance(listed, dict):
        raise ConfigError(f"{i_where}: must be a mapping of instrument profile files")
    if instrument_file not in listed:
        raise ConfigError(
            f"{i_where}: no research values for {instrument_file!r} (listed: {sorted(listed)})"
        )
    p_where = f"{i_where}: {instrument_file}"
    per = _check_keys(
        listed[instrument_file],
        {"reference_price", "round_trip_per_contract", "strike_step"},
        p_where,
    )
    reference = per["reference_price"]
    if per["strike_step"] is not None:
        strike_step = _positive_decimal(per, "strike_step", p_where)
    option = OptionSettings(
        moneyness_points=_signed(o, "moneyness_points", o_where),
        days_to_expiry=float(_positive_decimal(o, "days_to_expiry", o_where)),
        days_per_year=float(_positive_decimal(o, "days_per_year", o_where)),
        rate=_signed(o, "rate", o_where),
        reference_price=None
        if reference is None
        else float(_positive_decimal(per, "reference_price", p_where)),
        strike_step=float(strike_step),
        point_value=float(instrument.point_value),
    )

    cost = _signed(per, "round_trip_per_contract", p_where)
    if cost < 0:
        raise ConfigError(f"{p_where}: round_trip_per_contract must be >= 0, got {cost}")

    e_where = f"{where}: exits"
    e = _check_keys(raw["exits"], {"booker", "scale_out", "adr_bracket"}, e_where)
    booker = _check_keys(e["booker"], {"contracts"}, f"{e_where}: booker")
    scale = _check_keys(e["scale_out"], {"contracts"}, f"{e_where}: scale_out")
    booker_contracts = _integer(booker, "contracts", f"{e_where}: booker", minimum=1)
    scale_contracts = _integer(scale, "contracts", f"{e_where}: scale_out", minimum=1)
    if scale_contracts <= 1:
        raise ConfigError(f"{e_where}: scale_out needs more than one contract")
    a_where = f"{e_where}: adr_bracket"
    a = _check_keys(
        e["adr_bracket"], {"contracts", "stop_range_fraction", "target_range_fraction"}, a_where
    )
    adr_bracket = AdrBracketSettings(
        contracts=_integer(a, "contracts", a_where, minimum=1),
        stop_range_fraction=float(_positive_decimal(a, "stop_range_fraction", a_where)),
        target_range_fraction=float(_positive_decimal(a, "target_range_fraction", a_where)),
    )
    exits = ExitSettings(
        stop_dollars=float(risk.stop_loss),
        lock_start_dollars=float(risk.profit_target),
        lock_fraction=float(risk.trail_keep_pct / _PERCENT_MAX),
        first_target_dollars=float(risk.profit_target),
        booker_contracts=booker_contracts,
        scale_out_contracts=scale_contracts,
        adr_bracket=adr_bracket,
    )

    j_where = f"{where}: judging"
    j = _check_keys(raw["judging"], {"time_parts", "min_positive_parts", "min_t_stat"}, j_where)
    t_where = f"{j_where}: min_t_stat"
    bars = j["min_t_stat"]
    if not isinstance(bars, dict):
        raise ConfigError(f"{t_where}: must be a mapping of rule names to numbers")
    judging = JudgingSettings(
        time_parts=_integer(j, "time_parts", j_where, minimum=1),
        min_positive_parts=_integer(j, "min_positive_parts", j_where, minimum=0),
        min_t_stat={str(name): _signed(bars, name, t_where) for name in bars},
    )
    if judging.min_positive_parts > judging.time_parts:
        raise ConfigError(f"{j_where}: min_positive_parts must be <= time_parts")

    rules = raw["rules"]
    if not isinstance(rules, dict):
        raise ConfigError(f"{where}: rules must be a mapping")
    return ResearchSettings(
        timezone=timezone,
        roll=roll,
        sessions=sessions,
        vol_divisor=divisor,
        vol_max_age_days=max_age,
        option=option,
        round_trip_per_contract=cost,
        exits=exits,
        judging=judging,
        gates=gates,
        bankroll=float(risk.bankroll),
        symbol=instrument.symbol,
        rules=rules,
    )
