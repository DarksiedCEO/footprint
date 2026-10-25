"""Backtest settings, loaded from config/backtest.yaml. No number lives in code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from footprint.backtest.bollinger import BollingerSettings
from footprint.backtest.exits import TimeExitSettings
from footprint.backtest.pullback import PullbackSettings
from footprint.backtest.stairstep import StairSettings
from footprint.core.config import (
    _PERCENT_MAX,
    ConfigError,
    _check_keys,
    _integer,
    _positive_decimal,
    _read_mapping,
)
from footprint.core.options import is_trading_class

_MINUTE_SECONDS = int(timedelta(minutes=1).total_seconds())


@dataclass(frozen=True, slots=True)
class OptionModelSettings:
    atm_premium_points: Decimal
    spread_points: Decimal
    strike_step: Decimal
    strikes_each_side: int
    open_interest: int
    expiries_listed: int
    trading_class: str  # the class the modelled (European, daily) options carry
    # The futures level options are modelled at (BH-2; footprint.backtest.price_level);
    # None: no rescaling, the model at the data's own price.
    reference_price: Decimal | None


@dataclass(frozen=True, slots=True)
class EngineSettings:
    substep_seconds: int
    history_minutes: int


@dataclass(frozen=True, slots=True)
class GateSettings:
    min_trades: int
    min_win_rate_pct: Decimal
    min_payoff_ratio: Decimal
    min_profit_factor: Decimal
    max_drawdown_pct: Decimal
    out_of_sample_pct: Decimal
    min_out_of_sample_trades: int


@dataclass(frozen=True, slots=True)
class RuleSettings:
    """The Q5 rule library's numbers (docs/strategy-rules.md). Backtest only."""

    stairstep: StairSettings
    pullback: PullbackSettings
    time: TimeExitSettings
    bollinger: BollingerSettings


@dataclass(frozen=True, slots=True)
class RollSettings:
    """Contract rolls in a raw front-month history (BH-9; footprint.backtest.rolls)."""

    block_first_session: bool  # no entry on the first session on a new contract


@dataclass(frozen=True, slots=True)
class BacktestSettings:
    option_model: OptionModelSettings
    engine: EngineSettings
    gates: GateSettings
    rules: RuleSettings
    rolls: RollSettings


def _trading_class(raw: Mapping[str, object], key: str, where: str) -> str:
    value = raw[key]
    if not is_trading_class(value):
        raise ConfigError(f"{where}: {key} must be a non-empty name with no spaces, got {value!r}")
    return value


def load_backtest(path: Path) -> BacktestSettings:
    where = str(path)
    raw = _read_mapping(path, {f.name for f in fields(BacktestSettings)})

    o_where = f"{where}: option_model"
    o = _check_keys(raw["option_model"], {f.name for f in fields(OptionModelSettings)}, o_where)
    option_model = OptionModelSettings(
        atm_premium_points=_positive_decimal(o, "atm_premium_points", o_where),
        spread_points=_positive_decimal(o, "spread_points", o_where),
        strike_step=_positive_decimal(o, "strike_step", o_where),
        strikes_each_side=_integer(o, "strikes_each_side", o_where, minimum=1),
        open_interest=_integer(o, "open_interest", o_where, minimum=0),
        expiries_listed=_integer(o, "expiries_listed", o_where, minimum=1),
        trading_class=_trading_class(o, "trading_class", o_where),
        reference_price=None
        if o["reference_price"] is None
        else _positive_decimal(o, "reference_price", o_where),
    )

    e_where = f"{where}: engine"
    e = _check_keys(raw["engine"], {f.name for f in fields(EngineSettings)}, e_where)
    engine = EngineSettings(
        substep_seconds=_integer(e, "substep_seconds", e_where, minimum=1),
        history_minutes=_integer(e, "history_minutes", e_where, minimum=1),
    )
    if engine.substep_seconds >= _MINUTE_SECONDS or _MINUTE_SECONDS % engine.substep_seconds:
        raise ConfigError(f"{e_where}: substep_seconds must divide a minute")

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
    rules = _rules(raw["rules"], where)
    b = rules.bollinger
    need = b.history * max(b.fast_minutes, b.slow_minutes)
    if engine.history_minutes < need:
        raise ConfigError(
            f"{e_where}: history_minutes must cover the bollinger rules ({need} minutes)"
        )
    r_where = f"{where}: rolls"
    r = _check_keys(raw["rolls"], {f.name for f in fields(RollSettings)}, r_where)
    block = r["block_first_session"]
    if not isinstance(block, bool):
        raise ConfigError(f"{r_where}: block_first_session must be true or false, got {block!r}")
    return BacktestSettings(
        option_model=option_model,
        engine=engine,
        gates=gates,
        rules=rules,
        rolls=RollSettings(block_first_session=block),
    )


def _rules(raw: object, where: str) -> RuleSettings:
    r_where = f"{where}: rules"
    r = _check_keys(raw, {f.name for f in fields(RuleSettings)}, r_where)
    s_where = f"{r_where}: stairstep"
    s = _check_keys(r["stairstep"], {f.name for f in fields(StairSettings)}, s_where)
    steps = _integer(s, "steps", s_where, minimum=1)
    max_giveback = _positive_decimal(s, "max_giveback", s_where)
    max_pause_bars = _integer(s, "max_pause_bars", s_where, minimum=1)
    max_stair_bars = _integer(s, "max_stair_bars", s_where, minimum=1)
    try:  # StairSettings checks the rest (max_giveback at most 1)
        stairstep = StairSettings(steps, max_giveback, max_pause_bars, max_stair_bars)
    except ValueError as exc:
        raise ConfigError(f"{s_where}: {exc}") from exc
    p_where = f"{r_where}: pullback"
    p = _check_keys(r["pullback"], {f.name for f in fields(PullbackSettings)}, p_where)
    pullback = PullbackSettings(
        lifetime_minutes=_integer(p, "lifetime_minutes", p_where, minimum=1)
    )
    t_where = f"{r_where}: time"
    t = _check_keys(r["time"], {f.name for f in fields(TimeExitSettings)}, t_where)
    time = TimeExitSettings(
        minutes=_integer(t, "minutes", t_where, minimum=1),
        min_gain=_positive_decimal(t, "min_gain", t_where),
    )
    b_where = f"{r_where}: bollinger"
    b = _check_keys(r["bollinger"], {f.name for f in fields(BollingerSettings)}, b_where)
    counts = {
        f.name: _integer(b, f.name, b_where, minimum=1)
        for f in fields(BollingerSettings)
        if f.name != "width"
    }
    try:  # BollingerSettings checks the rest (period above one, closes within lookback)
        bollinger = BollingerSettings(width=_positive_decimal(b, "width", b_where), **counts)
    except ValueError as exc:
        raise ConfigError(f"{b_where}: {exc}") from exc
    return RuleSettings(stairstep=stairstep, pullback=pullback, time=time, bollinger=bollinger)
