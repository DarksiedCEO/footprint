"""Modelled option quotes for a backtest (G4).

There is no historical options data, so every quote is computed from the futures
price with the strategy's fixed, pessimistic delta:

    mid = time value + delta * (F - K)      (calls; puts use K - F)
    time value = atm_premium_points * sqrt(days to expiry)

so time value decays toward expiry (theta), a move of one point in the future moves
the premium by `delta` points (no gamma: pessimistic for a winner), and every trade
pays the full `spread_points` (bid below the mid, ask above it, both on the option
tick). Expiries are daily, on trading days; an expiry ends at the prime session's
close. The same moment and futures price always give the same quotes, so the chain
the decision sees and the quote the broker fills against are identical (R12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

from footprint.backtest.settings import OptionModelSettings
from footprint.core.calendar import is_trading_day, prime_close, trading_day_at
from footprint.core.config import InstrumentProfile
from footprint.core.expiry import is_european_class
from footprint.core.models import require_utc
from footprint.core.options import OptionChain, OptionQuote, Right
from footprint.execution.orders import Quote

_DAY_SECONDS = Decimal(int(timedelta(days=1).total_seconds()))


@dataclass(frozen=True, slots=True)
class ContractSpec:
    right: Right
    strike: Decimal
    expiry: date


class OptionModel:
    def __init__(
        self, model: OptionModelSettings, instrument: InstrumentProfile, delta: Decimal
    ) -> None:
        if not Decimal(0) < delta < Decimal(1):
            raise ValueError("delta must be between 0 and 1")
        if not is_european_class(model.trading_class, instrument):
            raise ValueError(
                f"option_model trading_class {model.trading_class!r} is not one of the "
                "instrument's European classes: the model prices European options only"
            )
        self._m = model
        self._instrument = instrument
        self._delta = delta
        self._tick = instrument.option_tick_size
        self._zone = ZoneInfo(instrument.prime_session.timezone)
        self._specs: dict[str, ContractSpec] = {}

    def chain(self, now: datetime, underlying_price: Decimal) -> OptionChain:
        """Every listed contract's quote at `now` for futures price `underlying_price`."""
        require_utc(now, "now")
        step = self._m.strike_step
        atm = (underlying_price / step).to_integral_value(ROUND_HALF_EVEN) * step
        strikes = [
            atm + step * k
            for k in range(-self._m.strikes_each_side, self._m.strikes_each_side + 1)
            if atm + step * k > 0
        ]
        quotes: list[OptionQuote] = []
        for expiry in self._expiries(now):
            for strike in strikes:
                for right in Right:
                    contract_id = f"{right.value}{strike}-{expiry:%Y%m%d}"
                    self._specs[contract_id] = ContractSpec(right, strike, expiry)
                    bid, ask = self._prices(right, strike, expiry, now, underlying_price)
                    quotes.append(
                        OptionQuote(
                            contract_id,
                            expiry,
                            strike,
                            right,
                            bid,
                            ask,
                            self._m.open_interest,
                            self._m.trading_class,
                        )
                    )
        return OptionChain(self._instrument.symbol, now, underlying_price, tuple(quotes))

    def quote(self, contract_id: str, now: datetime, underlying_price: Decimal) -> Quote:
        """The broker-side quote of one contract this model has listed."""
        require_utc(now, "now")
        spec = self._specs[contract_id]
        bid, ask = self._prices(spec.right, spec.strike, spec.expiry, now, underlying_price)
        return Quote(contract_id, bid, ask, now)

    def broker_quotes(self, chain: OptionChain) -> list[Quote]:
        return [Quote(q.contract_id, q.bid, q.ask, chain.ts) for q in chain.quotes]

    def _expiries(self, now: datetime) -> list[date]:
        today = trading_day_at(now, self._instrument) or now.astimezone(self._zone).date()
        found: list[date] = []
        day = today
        while len(found) < self._m.expiries_listed:
            day += timedelta(days=1)
            if is_trading_day(day, self._instrument):
                found.append(day)
        return found

    def _prices(
        self, right: Right, strike: Decimal, expiry: date, now: datetime, price: Decimal
    ) -> tuple[Decimal, Decimal]:
        close = datetime.combine(expiry, prime_close(expiry, self._instrument), self._zone)
        days_left = Decimal(int((close - now).total_seconds())) / _DAY_SECONDS
        time_value = self._m.atm_premium_points * days_left.sqrt() if days_left > 0 else Decimal(0)
        moneyness = price - strike if right is Right.CALL else strike - price
        mid = max(self._tick, time_value + self._delta * moneyness)
        half = self._m.spread_points / 2
        bid = self._on_tick(mid - half, ROUND_FLOOR)
        ask = max(self._tick, self._on_tick(mid + half, ROUND_CEILING))
        return max(Decimal(0), bid), ask

    def _on_tick(self, value: Decimal, rounding: str) -> Decimal:
        return (value / self._tick).to_integral_value(rounding) * self._tick
