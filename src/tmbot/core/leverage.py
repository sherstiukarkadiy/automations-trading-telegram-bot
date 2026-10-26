"""Leverage selection.

- Leverage comes from the signal; without it the exchange maximum for the asset is used.
- It is clamped to the exchange maximum.
- The liquidation price must sit beyond the stop with a margin; otherwise leverage is lowered.

Liquidation is approximated for isolated margin:
    LONG:  liq ≈ entry · (1 − 1/L + mmr)
    SHORT: liq ≈ entry · (1 + 1/L − mmr)
This is a pre-trade estimate; once the position is open, the worker compares it with the
liquidation price reported by the exchange (cross margin can differ noticeably).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tmbot.core.errors import TradeSetupError
from tmbot.core.models import Instrument, Side

DEFAULT_LIQ_BUFFER = Decimal("1.2")  # distance to liquidation ≥ 1.2 × distance to the stop


def liquidation_price(side: Side, entry: Decimal, leverage: int, mmr: Decimal) -> Decimal:
    inverse = Decimal(1) / Decimal(leverage)
    if side is Side.LONG:
        return entry * (1 - inverse + mmr)
    return entry * (1 + inverse - mmr)


@dataclass(frozen=True, slots=True)
class LeverageDecision:
    leverage: int
    requested: int | None
    clamped_to_exchange: bool  # the requested value exceeded the exchange maximum
    lowered_for_liquidation: bool  # lowered so that liquidation sits beyond the stop
    liquidation_price: Decimal
    margin_required: Decimal

    @property
    def notes(self) -> list[str]:
        """User-facing explanations of any adjustment."""
        out: list[str] = []
        if self.clamped_to_exchange:
            out.append(f"плече обрізано до максимуму біржі {self.leverage}x")
        if self.lowered_for_liquidation:
            out.append(f"плече знижено до {self.leverage}x, щоб ліквідація була далі за SL")
        return out


def choose_leverage(
    side: Side,
    avg_entry: Decimal,
    stop: Decimal,
    notional: Decimal,
    instrument: Instrument,
    requested: int | None,
    liq_buffer: Decimal = DEFAULT_LIQ_BUFFER,
) -> LeverageDecision:
    """Highest allowed leverage that keeps liquidation beyond the stop.

    Raises ``TradeSetupError`` if even 1x would liquidate before the stop.
    """
    cap = instrument.max_leverage
    start = cap if requested is None else max(min(requested, cap), 1)
    required_distance = abs(avg_entry - stop) * liq_buffer

    for leverage in range(start, 0, -1):
        liq = liquidation_price(side, avg_entry, leverage, instrument.maintenance_margin_rate)
        if side.sign * (avg_entry - liq) >= required_distance:
            return LeverageDecision(
                leverage=leverage,
                requested=requested,
                clamped_to_exchange=requested is not None and requested > cap,
                lowered_for_liquidation=leverage < start,
                liquidation_price=liq,
                margin_required=notional / leverage,
            )
    raise TradeSetupError("навіть при 1x ліквідація ближча за SL: стоп занадто далекий")
