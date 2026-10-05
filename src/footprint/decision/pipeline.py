"""The entry decision, end to end: bars in, at most one approved entry out.

Order (one-way, nothing loops back):
0. risk precheck (Dolores): breakers, kill switch, open position, trade ceiling,
   session and news, run on EVERY decision so the record always shows them;
1. crew reads on completed bars only: trend (Mateo), momentum (Darius), volume
   (Rafael), zero-day (Nia), levels (Grant), daily range (Nadia);
2. console gates and score (Desmond);
3. contract (Felix) -> size (Ezra) -> full risk veto with the sized cost (Dolores);
4. an ApprovedEntry only when every step passes.
Every step's result is kept on the Decision so the journal can record why the
system did or did not trade. Malformed input (wrong symbol, out-of-order bars)
is a failed decision, never a crash and never a trade. Nothing here places an
order: execution accepts only an ApprovedEntry.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

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
from footprint.core.series import require_series
from footprint.data.aggregate import completed, resample, session_days
from footprint.decision.console.desmond import Console, ConsoleInputs, ConsoleVerdict
from footprint.decision.risk.gate import (
    AccountState,
    EntryRequest,
    RiskDecision,
    evaluate,
    precheck,
)
from footprint.decision.session import NewsBlackout
from footprint.decision.sizing import EntryPlan, NoSize, size

_APPROVAL = object()


@dataclass(frozen=True, slots=True)
class ApprovedEntry:
    """The only thing execution may act on. Built by the pipeline alone, after the
    console, contract, sizing and the full risk check all passed.
    """

    signal: Signal
    plan: EntryPlan
    entry: EntryRequest
    risk: RiskDecision
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _APPROVAL:
            raise PermissionError("only the decision pipeline can approve an entry")
        if not self.risk.allowed:
            raise PermissionError("an entry the risk layer vetoed cannot be approved")
        if self.entry.cost != self.plan.cost or self.entry.direction is not self.signal.direction:
            raise PermissionError("approval does not match the sized plan")


@dataclass(frozen=True, slots=True)
class Decision:
    as_of: datetime
    precheck: RiskDecision
    verdict: ConsoleVerdict | None
    data_error: str | None
    contract: ContractChoice | NoContract | None
    plan: EntryPlan | NoSize | None
    entry: EntryRequest | None
    risk: RiskDecision | None
    approved: ApprovedEntry | None

    @property
    def fired(self) -> bool:
        return self.approved is not None

    @property
    def latch_kill_switch(self) -> bool:
        return self.precheck.latch_kill_switch

    def reasons(self) -> list[str]:
        """Why there is no approved entry (empty when it fired)."""
        if self.fired:
            return []
        out = ["risk: " + v.value for v in self.precheck.vetoes]
        if self.data_error:
            out.append(f"data: {self.data_error}")
        if self.verdict is not None:
            out += [f"{g.name}: {g.detail}" for g in self.verdict.failed]
        if isinstance(self.contract, NoContract):
            out.append(f"contract: {self.contract.reason}")
        if isinstance(self.plan, NoSize):
            out.append(f"sizing: {self.plan.reason}")
        if self.risk is not None:
            out += ["risk: " + v.value for v in self.risk.vetoes if v not in self.precheck.vetoes]
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
        blackouts = tuple(blackouts)
        pre = precheck(p.risk, p.instrument, blackouts, account, as_of)

        def stop(
            verdict: ConsoleVerdict | None = None,
            data_error: str | None = None,
            contract: ContractChoice | NoContract | None = None,
            plan: EntryPlan | NoSize | None = None,
            entry: EntryRequest | None = None,
            risk: RiskDecision | None = None,
            approved: ApprovedEntry | None = None,
        ) -> Decision:
            return Decision(as_of, pre, verdict, data_error, contract, plan, entry, risk, approved)

        try:
            verdict = self._verdict(minute_bars, as_of)
        except (ValueError, TypeError) as exc:
            return stop(data_error=str(exc))
        if not verdict.go or verdict.direction is None or not pre.allowed:
            return stop(verdict=verdict)

        direction = verdict.direction
        choice = (
            self._contract.choose(chain, direction, as_of)
            if chain is not None
            else NoContract("no option chain")
        )
        if isinstance(choice, NoContract):
            return stop(verdict=verdict, contract=choice)
        plan = size(choice, verdict.session, p.risk, p.instrument, p.execution)
        if isinstance(plan, NoSize):
            return stop(verdict=verdict, contract=choice, plan=plan)
        entry = EntryRequest(as_of, direction, plan.cost)
        risk = evaluate(p.risk, p.instrument, blackouts, account, entry)
        if not risk.allowed:
            return stop(verdict=verdict, contract=choice, plan=plan, entry=entry, risk=risk)
        signal = Signal(ts=as_of, direction=direction, reads=verdict.reads)
        approved = ApprovedEntry(signal, plan, entry, risk, _APPROVAL)
        return stop(
            verdict=verdict, contract=choice, plan=plan, entry=entry, risk=risk, approved=approved
        )

    def _verdict(self, minute_bars: Sequence[Bar], as_of: datetime) -> ConsoleVerdict:
        p = self._p
        minutes = completed(minute_bars, as_of, 1)
        if minutes:
            require_series(minutes, 1, "minute bars")
            if minutes[0].symbol != p.instrument.symbol:
                raise ValueError(
                    f"minute bars are for {minutes[0].symbol!r}, not the traded instrument"
                )
            trend_bars = resample(minutes, p.strategy.trend.timeframe_minutes, as_of)
            trend = self._trend.read(trend_bars) if trend_bars else _blind("trend", as_of)
            momentum = self._momentum.read(minutes)
            volume = self._volume.read(minutes)
        else:
            trend = _blind("trend", as_of)
            momentum = _blind("momentum", as_of)
            volume = _blind("volume", as_of)
        return self._console.evaluate(
            ConsoleInputs(
                as_of=as_of,
                trend=trend,
                momentum=momentum,
                volume=volume,
                zero_day=self._zero_day.check(minutes, as_of),
                levels=self._levels.snapshot(minutes, as_of),
                range_days=self._range_days(minutes, as_of),
                n_points=self._n_points,
            )
        )

    def _range_days(self, minutes: Sequence[Bar], as_of: datetime) -> list[Bar] | None:
        """Completed cash days plus today's cash session (in progress, or finished if
        the cash session has already closed). None before today's cash open.
        """
        cash = self._p.instrument.cash_session
        days = session_days(minutes, cash, as_of)
        if days.today is not None:
            return [*days.days, days.today]
        if days.days:
            zone = ZoneInfo(cash.timezone)
            if days.days[-1].ts.astimezone(zone).date() == as_of.astimezone(zone).date():
                return list(days.days)
        return None


def _blind(agent_id: str, as_of: datetime) -> Read:
    return Read(agent_id, as_of, Score.NEUTRAL, "no completed bars")
