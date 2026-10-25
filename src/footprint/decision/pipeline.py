"""The entry decision, end to end: bars in, at most one approved entry out.

Order (one-way, nothing loops back):
0. risk precheck (Dolores): breakers, kill switch, open position, trade ceiling,
   session and news, run on EVERY decision so the record always shows them;
0b. the play caller (decision/play.py), also on EVERY decision so the day's label is
   made at the decision minute whatever else happens: a TREND day runs the breakout
   entry; a CHOP or STAND ASIDE day, and any moment before the decision time, takes no
   new entries (PC-5). Exits never pass through here (the runner asks for a decision
   only with nothing open or working);
1. crew reads on completed bars only: trend (Mateo), momentum (Darius), volume
   (Rafael), zero-day (Nia), levels (Grant), daily range (Nadia);
2. console gates and score (Desmond);
3. contract (Felix) -> size (Ezra) -> decay veto (Theta) -> the Greeks vetoes (Vega,
   Delta, Gamma; GV-1) -> full risk veto with the sized cost (Dolores), only when
   the play allows entries;
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
from typing import Protocol
from zoneinfo import ZoneInfo

from footprint.agents.contract_picker import ContractChoice, ContractPicker, NoContract
from footprint.agents.greeks import DeltaAgent, GammaAgent, GreekCheck, VegaAgent
from footprint.agents.levels import LevelsReader, points_for_target
from footprint.agents.momentum_reader import MomentumReader
from footprint.agents.range_reader import RangeReader
from footprint.agents.theta import ThetaAgent, ThetaCheck
from footprint.agents.trend_reader import TrendReader
from footprint.agents.volume_reader import VolumeReader
from footprint.agents.zero_day import ZeroDayAgent
from footprint.core.config import Profiles
from footprint.core.models import Bar, Read, Score, Signal, require_utc
from footprint.core.options import OptionChain, Right
from footprint.core.series import require_series
from footprint.core.timecache import local_time
from footprint.data.aggregate import completed, resample, session_days
from footprint.decision.console.desmond import Console, ConsoleInputs, ConsoleVerdict
from footprint.decision.play import PlayCall, PlayCaller, PlayGate
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
    theta: ThetaCheck
    vega: GreekCheck
    delta: GreekCheck
    gamma: GreekCheck
    play: PlayCall
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _APPROVAL:
            raise PermissionError("only the decision pipeline can approve an entry")
        if not self.risk.allowed:
            raise PermissionError("an entry the risk layer vetoed cannot be approved")
        if self.risk.entry != self.entry:
            raise PermissionError("the risk layer judged a different entry")
        if self.entry.cost != self.plan.cost or self.entry.direction is not self.signal.direction:
            raise PermissionError("approval does not match the sized plan")
        if self.entry.ts != self.signal.ts:
            raise PermissionError("approval and signal are from different moments")
        if self.plan.choice.quote.right is not Right.for_direction(self.signal.direction):
            raise PermissionError("the contract's right does not match the direction")
        if not self.theta.allowed:
            raise PermissionError("an entry theta vetoed cannot be approved")
        if (self.theta.contract_id, self.theta.contracts) != (
            self.plan.choice.quote.contract_id,
            self.plan.contracts,
        ):
            raise PermissionError("theta judged a different contract or size")
        for name, check in (("vega", self.vega), ("delta", self.delta), ("gamma", self.gamma)):
            if check.agent_id != name:
                raise PermissionError(
                    f"the {name} slot holds {check.agent_id!r}: not a {name} check"
                )
            if not check.allowed:
                raise PermissionError(f"an entry {name} vetoed cannot be approved")
            if (check.contract_id, check.contracts) != (
                self.plan.choice.quote.contract_id,
                self.plan.contracts,
            ):
                raise PermissionError(f"{name} judged a different contract or size")
        if not self.play.entries_allowed:
            raise PermissionError("no new entries on this play (only a TREND day trades)")


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
    theta: ThetaCheck | None = None
    vega: GreekCheck | None = None
    delta: GreekCheck | None = None
    gamma: GreekCheck | None = None
    play: PlayCall | None = None

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
        if self.play is not None and not self.play.entries_allowed:
            out.append(f"play: {self.play.reason}")
        if self.data_error:
            out.append(f"data: {self.data_error}")
        if self.verdict is not None:
            out += [f"{g.name}: {g.detail}" for g in self.verdict.failed]
        if isinstance(self.contract, NoContract):
            out.append(f"contract: {self.contract.reason}")
        if isinstance(self.plan, NoSize):
            out.append(f"sizing: {self.plan.reason}")
        if self.theta is not None and not self.theta.allowed:
            out.append(f"theta: {self.theta.reason}")
        for check in (self.vega, self.delta, self.gamma):
            if check is not None and not check.allowed:
                out.append(f"{check.agent_id}: {check.reason}")
        if self.risk is not None:
            out += ["risk: " + v.value for v in self.risk.vetoes if v not in self.precheck.vetoes]
        return out


class EntryTrigger(Protocol):
    """The momentum read: completed entry-timeframe bars in, a Read out."""

    def read(self, bars: Sequence[Bar]) -> Read: ...


class Gatekeeper(Protocol):
    """The console's job: the crew's reads in, a go / no-go verdict out."""

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict: ...


class Pipeline:
    def __init__(
        self,
        profiles: Profiles,
        trigger: EntryTrigger | None = None,
        console: Gatekeeper | None = None,
        play: PlayGate | None = None,
    ) -> None:
        """`trigger` replaces only the momentum read (backtest rule library); None, as
        the live runner builds it, is the momentum reader (Darius). `console` replaces
        the console (backtest rule library only); None, as the live runner builds it,
        is the live console (Desmond). `play` replaces the play caller (the backtest builds
        one on its own history source); None, as the live runner builds it, is the play
        caller reading the bars handed to each decision."""
        self._p = profiles
        s, inst = profiles.strategy, profiles.instrument
        self._trend = TrendReader(s.trend)
        self._momentum: EntryTrigger = (
            MomentumReader(s.volume, inst) if trigger is None else trigger
        )
        self._volume = VolumeReader(s.volume, inst)
        self._zero_day = ZeroDayAgent(s.zeroday, s.data, inst)
        self._levels = LevelsReader(s.levels, s.trend, s.data, inst)
        self._range = RangeReader(s.range)
        self._console: Gatekeeper = (
            Console(s.console, inst, self._levels, self._range) if console is None else console
        )
        self._contract = ContractPicker(profiles.execution, inst)
        self._theta = ThetaAgent(s.theta, inst, profiles.risk)
        # GV-1: one rate for every Black-76 valuation (Theta's), one volatility band
        # (Vega's) that every Greeks agent solves in. Run in this order, after Theta.
        self._greeks = (
            VegaAgent(s.vega, s.theta.rate, inst),
            DeltaAgent(s.delta, s.vega, s.theta.rate, inst),
            GammaAgent(s.gamma, s.vega, s.theta.rate, inst),
        )
        self._play: PlayGate = (
            PlayCaller(profiles.regime, profiles.play, s.data, inst) if play is None else play
        )
        self._issued: dict[int, ApprovedEntry] = {}
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
            theta: ThetaCheck | None = None,
            greeks: Sequence[GreekCheck] = (),
        ) -> Decision:
            found = {check.agent_id: check for check in greeks}
            return Decision(
                as_of,
                pre,
                verdict,
                data_error,
                contract,
                plan,
                entry,
                risk,
                approved,
                theta,
                vega=found.get("vega"),
                delta=found.get("delta"),
                gamma=found.get("gamma"),
                play=play,
            )

        # Asked first and on every decision: the day's label is fixed at the decision
        # minute, whatever the crew, the data or the risk layer say.
        play = self._play.call(minute_bars, as_of)
        try:
            verdict = self._verdict(minute_bars, as_of)
        except (ValueError, TypeError) as exc:
            return stop(data_error=str(exc))
        if (
            not verdict.go
            or verdict.direction is None
            or not pre.allowed
            or not play.entries_allowed
        ):
            return stop(verdict=verdict)

        direction = verdict.direction
        if chain is None:
            return stop(verdict=verdict, contract=NoContract("no option chain"))
        choice = self._contract.choose(chain, direction, as_of)
        if isinstance(choice, NoContract):
            return stop(verdict=verdict, contract=choice)
        plan = size(choice, verdict.session, p.risk, p.instrument, p.execution)
        if isinstance(plan, NoSize):
            return stop(verdict=verdict, contract=choice, plan=plan)
        theta = self._theta.assess(plan, chain.underlying_price, as_of)
        if not theta.allowed:
            return stop(verdict=verdict, contract=choice, plan=plan, theta=theta)
        greeks: list[GreekCheck] = []
        for agent in self._greeks:
            check = agent.assess(plan, chain.underlying_price, as_of)
            greeks.append(check)
            if not check.allowed:
                return stop(verdict=verdict, contract=choice, plan=plan, theta=theta, greeks=greeks)
        vega, delta, gamma = greeks
        entry = EntryRequest(as_of, direction, plan.cost)
        risk = evaluate(p.risk, p.instrument, blackouts, account, entry)
        if not risk.allowed:
            return stop(
                verdict=verdict,
                contract=choice,
                plan=plan,
                entry=entry,
                risk=risk,
                theta=theta,
                greeks=greeks,
            )
        signal = Signal(ts=as_of, direction=direction, reads=verdict.reads)
        approved = ApprovedEntry(
            signal, plan, entry, risk, theta, vega, delta, gamma, play, _APPROVAL
        )
        self._issued[id(approved)] = approved
        return stop(
            verdict=verdict,
            contract=choice,
            plan=plan,
            entry=entry,
            risk=risk,
            approved=approved,
            theta=theta,
            greeks=greeks,
        )

    def issued(self, approved: ApprovedEntry) -> bool:
        """True only for the exact object this pipeline approved. A copy, a
        dataclasses.replace() of it, or anything built elsewhere is not issued.
        """
        return self._issued.get(id(approved)) is approved

    def _verdict(self, minute_bars: Sequence[Bar], as_of: datetime) -> ConsoleVerdict:
        p = self._p
        # One immutable tuple shared by every reader this decision: the series helpers
        # cache their work per tuple (a tuple of frozen Bars cannot change underneath).
        minutes = tuple(completed(minute_bars, as_of, 1))
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
        """Completed cash days plus today. Today is the cash session so far; after the
        cash close it is the finished cash session extended by every bar since, so the
        range and the chase rule see the live price. None before today's cash open.
        """
        cash = self._p.instrument.cash_session
        days = session_days(minutes, cash, as_of)
        if days.today is not None:
            return [*days.days, days.today]
        zone = ZoneInfo(cash.timezone)
        today = as_of.astimezone(zone).date()
        if not days.days or days.days[-1].ts.astimezone(zone).date() != today:
            return None
        session = days.days[-1]
        after = [
            b
            for b in minutes
            if local_time(b.ts, cash.timezone).date() == today
            and local_time(b.ts, cash.timezone).time() >= cash.end
        ]
        if after:
            session = Bar(
                symbol=session.symbol,
                ts=session.ts,
                open=session.open,
                high=max(session.high, *(b.high for b in after)),
                low=min(session.low, *(b.low for b in after)),
                close=after[-1].close,
                volume=session.volume + sum(b.volume for b in after),
            )
        return [*days.days[:-1], session]


def _blind(agent_id: str, as_of: datetime) -> Read:
    return Read(agent_id, as_of, Score.NEUTRAL, "no completed bars")
