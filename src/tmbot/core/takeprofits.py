"""Splitting a position between take-profit levels.

Rules from the spec:
- Shares come from the signal; levels without a share split the remainder equally.
- A level the price has already passed is not placed; its share moves to the next level.
- The last level receives the rounding remainder, so the parts always sum to the size.
- A part smaller than the minimum lot is merged into the previous level.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tmbot.core.models import Instrument, Side, TakeProfit

# Shares like 1/3 are stored with 28 digits, so 3 × (1/3) = 0.999…; this nudge keeps
# round-down from losing a whole lot step. It cannot increase risk: the parts are
# capped by the total size, which the last level absorbs exactly.
_ROUNDING_NUDGE = Decimal("1e-12")


@dataclass(frozen=True, slots=True)
class TpOrder:
    price: Decimal
    qty: Decimal
    level: int  # 0-based index of the take-profit in the signal


@dataclass(frozen=True, slots=True)
class _Slot:
    level: int
    price: Decimal
    share: Decimal


def resolve_shares(take_profits: Sequence[TakeProfit]) -> list[Decimal]:
    """Shares for every level, summing to exactly 1."""
    if not take_profits:
        return []
    explicit = sum((tp.share for tp in take_profits if tp.share is not None), Decimal(0))
    if explicit > 1:
        raise ValueError("take-profit shares sum to more than 1")

    free = sum(1 for tp in take_profits if tp.share is None)
    if free:
        each = (Decimal(1) - explicit) / free
        shares = [tp.share if tp.share is not None else each for tp in take_profits]
    elif explicit > 0:
        # every share is given but they sum to less than 1: scale them up
        shares = [(tp.share or Decimal(0)) / explicit for tp in take_profits]
    else:
        raise ValueError("take-profit shares must be positive")

    shares[-1] = Decimal(1) - sum(shares[:-1], Decimal(0))  # absorb division error
    return shares


def _alive_slots(side: Side, take_profits: Sequence[TakeProfit], price: Decimal) -> list[_Slot]:
    """Levels the price has not reached yet; a passed level's share moves to the next one."""
    slots: list[_Slot] = []
    carried = Decimal(0)
    for level, (tp, share) in enumerate(
        zip(take_profits, resolve_shares(take_profits), strict=True)
    ):
        if side.is_beyond(price, tp.price):
            carried += share
            continue
        slots.append(_Slot(level, tp.price, share + carried))
        carried = Decimal(0)
    return slots


def _allocate(total_qty: Decimal, slots: list[_Slot], instrument: Instrument) -> list[TpOrder]:
    orders: list[TpOrder] = []
    allocated = Decimal(0)
    for i, slot in enumerate(slots):
        is_last = i == len(slots) - 1
        if is_last:
            qty = total_qty - allocated
        else:
            qty = instrument.round_qty_down(total_qty * slot.share + _ROUNDING_NUDGE)
            if qty < instrument.min_qty:
                continue  # too small on its own; the last level absorbs it
        if qty <= 0:
            continue
        if qty < instrument.min_qty and orders:
            previous = orders.pop()  # remainder too small for an order: merge into previous
            orders.append(TpOrder(previous.price, previous.qty + qty, previous.level))
        else:
            orders.append(TpOrder(slot.price, qty, slot.level))
        allocated += qty
    return orders


def plan_take_profits(
    side: Side,
    take_profits: Sequence[TakeProfit],
    qty: Decimal,
    instrument: Instrument,
    reference_price: Decimal,
) -> list[TpOrder]:
    """Take-profit orders for ``qty``, skipping levels already passed at ``reference_price``."""
    return _allocate(qty, _alive_slots(side, take_profits, reference_price), instrument)


def far_take_profit(side: Side, take_profits: Sequence[TakeProfit]) -> TakeProfit | None:
    """The farthest level in the trade direction; it is attached to every entry order."""
    if not take_profits:
        return None
    pick = max if side is Side.LONG else min
    return pick(take_profits, key=_price)


def _price(tp: TakeProfit) -> Decimal:
    return tp.price
