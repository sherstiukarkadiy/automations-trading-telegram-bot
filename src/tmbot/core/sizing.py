"""Position sizing with averaging across several entries.

The total size is chosen so that if every entry fills and the stop is hit, the account
loses exactly R (or slightly less, because quantities are rounded down):

    Q = R / Σ w_i·|E_i − SL|,   q_i = w_i·Q
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from tmbot.core.errors import TradeSetupError
from tmbot.core.models import Instrument, Side, Signal


@dataclass(frozen=True, slots=True)
class EntryLeg:
    """One entry after deciding whether it goes to the market or rests as a limit order."""

    price: Decimal  # for a market entry: the current price
    weight: Decimal
    is_market: bool


@dataclass(frozen=True, slots=True)
class SizedEntry:
    price: Decimal
    qty: Decimal
    is_market: bool

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


@dataclass(frozen=True, slots=True)
class PositionSize:
    entries: tuple[SizedEntry, ...]
    risk_target: Decimal
    risk_actual: Decimal  # loss at the stop with every entry filled, after rounding

    @property
    def total_qty(self) -> Decimal:
        return sum((e.qty for e in self.entries), Decimal(0))

    @property
    def notional(self) -> Decimal:
        return sum((e.notional for e in self.entries), Decimal(0))

    @property
    def avg_entry(self) -> Decimal:
        total = self.total_qty
        return self.notional / total if total else Decimal(0)

    def estimated_fees(self, stop: Decimal, fee_rate: Decimal) -> Decimal:
        """Entry plus exit at the stop, both as taker: the worst case."""
        return (self.notional + stop * self.total_qty) * fee_rate


def resolve_entries(signal: Signal, current_price: Decimal) -> list[EntryLeg]:
    """Decide the order type of every entry.

    - No entry prices: one market entry.
    - An entry the price has already reached (LONG: price <= entry, SHORT: mirrored)
      goes to the market; the others rest as limit orders.
    - Several market entries merge into one with the combined weight.
    """
    if signal.is_market:
        return [EntryLeg(price=current_price, weight=Decimal(1), is_market=True)]

    market_weight = Decimal(0)
    limits: list[EntryLeg] = []
    for price, weight in zip(signal.entries, signal.resolved_weights(), strict=True):
        if signal.side.is_beyond(price, current_price):
            market_weight += weight
        else:
            limits.append(EntryLeg(price=price, weight=weight, is_market=False))

    legs = [EntryLeg(current_price, market_weight, is_market=True)] if market_weight else []
    return legs + limits


def loss_at_stop(side: Side, entries: Iterable[SizedEntry], stop: Decimal) -> Decimal:
    """Loss (a positive number) when the whole size is closed at the stop, fees excluded."""
    return sum((side.sign * (e.price - stop) * e.qty for e in entries), Decimal(0))


def size_position(
    side: Side,
    legs: list[EntryLeg],
    stop: Decimal,
    risk: Decimal,
    instrument: Instrument,
) -> PositionSize:
    """Size every entry for the given risk. Raises ``TradeSetupError`` if impossible."""
    if risk <= 0:
        raise TradeSetupError("ризик має бути > 0")
    if not legs:
        raise TradeSetupError("немає входів")

    weighted_distance = Decimal(0)
    for leg in legs:
        distance = side.sign * (leg.price - stop)
        if distance <= 0:
            raise TradeSetupError(f"ціна входу {leg.price} вже за стопом {stop}")
        weighted_distance += leg.weight * distance

    total = risk / weighted_distance
    sized: list[SizedEntry] = []
    for leg in legs:
        raw_qty = leg.weight * total
        qty = instrument.round_qty_down(raw_qty)
        if qty < instrument.min_qty:
            raise TradeSetupError(
                f"обсяг {raw_qty:.8f} на вході {leg.price} менший за мінімальний лот "
                f"{instrument.min_qty}; збільш ризик або зменш кількість входів"
            )
        sized.append(SizedEntry(price=leg.price, qty=qty, is_market=leg.is_market))

    return PositionSize(
        entries=tuple(sized),
        risk_target=risk,
        risk_actual=loss_at_stop(side, sized, stop),
    )
