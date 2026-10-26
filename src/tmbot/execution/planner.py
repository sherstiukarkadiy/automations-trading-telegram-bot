"""Trade plan for one account: a signal plus the account's market data become concrete orders.

Pure code without network access: everything it needs (instrument, price, balance) is passed
in, so the plan is computed identically for the bot, the CLI and the tests.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from decimal import Decimal

from tmbot.core.errors import TradeSetupError
from tmbot.core.leverage import LeverageDecision, choose_leverage
from tmbot.core.models import Instrument, Side, Signal
from tmbot.core.sizing import SizedEntry, resolve_entries, size_position
from tmbot.core.takeprofits import TpOrder, far_take_profit, plan_take_profits

DEFAULT_FEE = Decimal("0.00055")  # Bybit taker fee for linear perpetuals, base tier


def new_trade_id() -> str:
    """Short unique id: 8 hex chars of time + 4 random (Bybit orderLinkId allows 36 chars)."""
    return f"{int(time.time()) & 0xFFFFFFFF:08x}{os.urandom(2).hex()}"


@dataclass(frozen=True, slots=True)
class TradePlan:
    trade_id: str
    signal: Signal  # prices already rounded to the instrument tick
    instrument: Instrument
    reference_price: Decimal
    entries: tuple[SizedEntry, ...]
    tp_full_fill: tuple[TpOrder, ...]  # take-profits if every entry fills
    leverage: LeverageDecision
    risk_target: Decimal
    risk_actual: Decimal
    fees: Decimal
    warnings: tuple[str, ...] = ()  # user-facing

    @property
    def asset(self) -> str:
        return self.instrument.asset

    @property
    def side(self) -> Side:
        return self.signal.side

    @property
    def stop_loss(self) -> Decimal:
        return self.signal.stop_loss

    @property
    def far_tp(self) -> Decimal | None:
        far = far_take_profit(self.side, self.signal.take_profits)
        return far.price if far else None

    @property
    def total_qty(self) -> Decimal:
        return sum((e.qty for e in self.entries), Decimal(0))

    @property
    def notional(self) -> Decimal:
        return sum((e.notional for e in self.entries), Decimal(0))

    @property
    def avg_entry(self) -> Decimal:
        """Average entry price if every entry fills."""
        total = self.total_qty
        return self.notional / total if total else Decimal(0)

    @property
    def has_limit_entries(self) -> bool:
        return any(not e.is_market for e in self.entries)

    def link_id(self, role: str, index: int) -> str:
        """Deterministic client order id, so a retried request cannot create a duplicate."""
        return f"tm{self.trade_id}-{role}{index}"

    def intermediate_take_profits(self, filled_qty: Decimal, price: Decimal) -> list[TpOrder]:
        """Take-profits for an already filled quantity, except the far one.

        The far level is attached to the position itself (full-position TP) and closes
        whatever remains, so it is never placed as a separate order.
        """
        far_level = len(self.signal.take_profits) - 1
        orders = plan_take_profits(
            self.side, self.signal.take_profits, filled_qty, self.instrument, price
        )
        return [o for o in orders if o.level != far_level]


def round_to_tick(signal: Signal, instrument: Instrument) -> Signal:
    return replace(
        signal,
        stop_loss=instrument.round_price(signal.stop_loss),
        entries=tuple(instrument.round_price(p) for p in signal.entries),
        take_profits=tuple(
            replace(tp, price=instrument.round_price(tp.price)) for tp in signal.take_profits
        ),
    )


def build_plan(
    signal: Signal,
    instrument: Instrument,
    price: Decimal,
    risk: Decimal,
    *,
    available_margin: Decimal | None = None,
    fee_rate: Decimal = DEFAULT_FEE,
    trade_id: str | None = None,
) -> TradePlan:
    """Turn a signal into a plan for one account. Raises ``TradeSetupError`` if impossible."""
    sig = round_to_tick(signal, instrument)
    issues = sig.validate()
    if issues:
        raise TradeSetupError("; ".join(map(str, issues)))
    if sig.side.is_beyond(sig.stop_loss, price):
        raise TradeSetupError(f"ціна {price} вже за стопом {sig.stop_loss}")
    far = far_take_profit(sig.side, sig.take_profits)
    if far is not None and sig.side.is_beyond(price, far.price):
        raise TradeSetupError("ціна вже пройшла всі тейки")

    size = size_position(sig.side, resolve_entries(sig, price), sig.stop_loss, risk, instrument)
    _check_min_notional(size.entries, instrument)
    leverage = choose_leverage(
        sig.side, size.avg_entry, sig.stop_loss, size.notional, instrument, sig.leverage
    )

    warnings = [*_passed_tp_warning(sig, price), *leverage.notes]
    if available_margin is not None and leverage.margin_required > available_margin:
        warnings.append(
            f"маржа {leverage.margin_required:.2f} більша за доступну {available_margin:.2f}"
        )

    return TradePlan(
        trade_id=trade_id or new_trade_id(),
        signal=sig,
        instrument=instrument,
        reference_price=price,
        entries=size.entries,
        tp_full_fill=tuple(
            plan_take_profits(sig.side, sig.take_profits, size.total_qty, instrument, price)
        ),
        leverage=leverage,
        risk_target=risk,
        risk_actual=size.risk_actual,
        fees=size.estimated_fees(sig.stop_loss, fee_rate),
        warnings=tuple(warnings),
    )


def _check_min_notional(entries: tuple[SizedEntry, ...], instrument: Instrument) -> None:
    if not instrument.min_notional:
        return
    for entry in entries:
        if entry.notional < instrument.min_notional:
            raise TradeSetupError(
                f"вартість входу {entry.notional:.2f} менша за мінімум біржі "
                f"{instrument.min_notional} {instrument.settle_coin}"
            )


def _passed_tp_warning(signal: Signal, price: Decimal) -> list[str]:
    passed = [
        str(level + 1)
        for level, tp in enumerate(signal.take_profits)
        if signal.side.is_beyond(price, tp.price)
    ]
    if not passed:
        return []
    return [f"тейки {', '.join(passed)} вже пройдені, їх частка переходить на наступні"]
