"""The entry decision, end to end: bars in, at most one approved Signal out.

Order (one-way, nothing loops back):
1. crew reads on completed bars only: trend (Mateo), momentum (Darius), volume
   (Rafael), zero-day (Nia), levels (Grant), daily range (Nadia);
2. console gates and score (Desmond);
3. contract (Felix) -> size (Ezra) -> risk veto (Dolores);
4. a Signal only when every step passes.
Every step's result is kept on the Decision so the journal can record why the
system did or did not trade. Nothing here places an order: execution acts only on
an approved Decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from footprint.agents.contract_picker import ContractChoice, ContractPicker, NoContract
from footprint.agents.levels import LevelsReader, points_for_target
from footprint.agents.momentum_reader import MomentumReader
from footprint.agents.range_reader import RangeReader
from footprint.agents.trend_reader import TrendReader
from footprint.agents.volume_reader import VolumeReader
from footprint.agents.zero_day import ZeroDayAgent
from footprint.core.config import Profiles
from footprint.core.models import Bar, Read, Score, Signal, require_utc
from footprint.core.options import OptionChain
from footprint.data.aggregate import completed, resample, session_days
from footprint.decision.console.desmond import Console, ConsoleInputs, ConsoleVerdict
from footprint.decision.risk.gate import (
    AccountState,
    EntryRequest,
    RiskDecision,
    drawdown_breached,
    evaluate,
)
from footprint.decision.session import NewsBlackout
from footprint.decision.sizing import EntryPlan, NoSize, size


@dataclass(frozen=True, slots=True)
class Decision:
    as_of: datetime
    verdict: ConsoleVerdict
    contract: ContractChoice | NoContract | None
    plan: EntryPlan | NoSize | None
    risk: RiskDecision | None
    signal: Signal | None
    latch_kill_switch: bool

    @property
    def fired(self) -> bool:
        return self.signal is not None

    def reasons(self) -> list[str]:
        """Why there is no signal (empty when it fired)."""
        if self.fired:
            return []
        out = [f"{g.name}: {g.detail}" for g in self.verdict.failed]
        if isinstance(self.contract, NoContract):
            out.append(f"contract: {self.contract.reason}")
        if isinstance(self.plan, NoSize):
            out.append(f"sizing: {self.plan.reason}")
        if self.risk is not None and not self.risk.allowed:
            out.append("risk: " + ", ".join(v.value for v in self.risk.vetoes))
        return out


class Pipeline:
    def __init__(self, profiles: Profiles) -> None:
        self._p = profiles
        s, inst = profiles.strategy, profiles.instrument
        self._trend = TrendReader(s.trend)
        self._momentum = MomentumReader(s.volume, inst)
        self._volume = VolumeReader(s.volume, inst)
        self._zero_day = ZeroDayAgent(s.zeroday, s.data, inst)
        self._levels = LevelsReader(s.levels, s.trend, s.data, inst)
        self._range = RangeReader(s.range)
        self._console = Console(s.console, inst, self._levels, self._range)
        self._contract = ContractPicker(profiles.execution, inst)
        self._n_points = points_for_target(
            profiles.risk.profit_target,
            s.options.assumed_delta,
            inst.point_value,
            profiles.risk.max_contracts,
        )

    def decide(
        self,
        minute_bars: Sequence[Bar],
        chain: OptionChain | None,
        account: AccountState,
        blackouts: Iterable[NewsBlackout],
        as_of: datetime,
    ) -> Decision:
        require_utc(as_of, "as_of")
        p = self._p
        minutes = completed(minute_bars, as_of, 1)
        latch = drawdown_breached(p.risk, account) and not account.kill_switch_latched

        if minutes:
            trend_bars = resample(minutes, p.strategy.trend.timeframe_minutes, as_of)
            trend = self._trend.read(trend_bars) if trend_bars else _blind("trend", as_of)
            momentum = self._momentum.read(minutes)
            volume = self._volume.read(minutes)
        else:
            trend = _blind("trend", as_of)
            momentum = _blind("momentum", as_of)
            volume = _blind("volume", as_of)
        days = session_days(minutes, p.instrument.cash_session, as_of)
        range_days = [*days.days, days.today] if days.today is not None else None

        verdict = self._console.evaluate(
            ConsoleInputs(
                as_of=as_of,
                trend=trend,
                momentum=momentum,
                volume=volume,
                zero_day=self._zero_day.check(minutes, as_of),
                levels=self._levels.snapshot(minutes, as_of),
                range_days=range_days,
                n_points=self._n_points,
            )
        )
        if not verdict.go or verdict.direction is None:
            return Decision(as_of, verdict, None, None, None, None, latch)

        direction = verdict.direction
        choice = (
            self._contract.choose(chain, direction, as_of)
            if chain is not None
            else NoContract("no option chain")
        )
        if isinstance(choice, NoContract):
            return Decision(as_of, verdict, choice, None, None, None, latch)
        plan = size(choice, verdict.session, p.risk, p.instrument, p.execution)
        if isinstance(plan, NoSize):
            return Decision(as_of, verdict, choice, plan, None, None, latch)
        risk = evaluate(
            p.risk, p.instrument, blackouts, account, EntryRequest(as_of, direction, plan.cost)
        )
        latch = latch or risk.latch_kill_switch
        if not risk.allowed:
            return Decision(as_of, verdict, choice, plan, risk, None, latch)
        signal = Signal(ts=as_of, direction=direction, reads=verdict.reads)
        return Decision(as_of, verdict, choice, plan, risk, signal, latch)


def _blind(agent_id: str, as_of: datetime) -> Read:
    return Read(agent_id, as_of, Score.NEUTRAL, "no completed bars")
