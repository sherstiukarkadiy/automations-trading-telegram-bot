"""Common exchange interface and the types shared by the core, execution and adapters.

Assets are always named in normalized form ("BTC"); translating them into exchange
symbols ("BTCUSDT" on Bybit, "BTC" on Propr) is the adapter's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from tmbot.core.models import Instrument, Side


class ExchangeError(Exception):
    """Base class for exchange errors. The message is user-facing."""

    def __init__(self, message: str, code: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code


class RetryableError(ExchangeError):
    """Transient failure (network, rate limit, 5xx) that persisted after the client's retries."""


class RejectedError(ExchangeError):
    """The exchange refused the request (parameters, balance, permissions); retrying won't help."""


class AuthError(RejectedError):
    """Invalid key or signature, unbound IP, or missing permissions."""


@dataclass(frozen=True, slots=True)
class Balance:
    coin: str
    realized: Decimal  # balance from closed trades, without floating P&L
    equity: Decimal  # realized + unrealized
    available: Decimal  # free to use as margin for a new position


@dataclass(frozen=True, slots=True)
class PositionInfo:
    asset: str
    side: Side
    size: Decimal
    avg_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    unrealized_pnl: Decimal
    liq_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def is_final(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True, slots=True)
class OrderAck:
    order_id: str
    link_id: str


@dataclass(frozen=True, slots=True)
class OrderState:
    order_id: str
    link_id: str
    status: OrderStatus
    qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal | None


@dataclass(frozen=True, slots=True)
class EntryOrder:
    """An entry; the stop and the far take-profit are attached for the whole position."""

    asset: str
    side: Side
    qty: Decimal
    price: Decimal | None  # None = market order
    stop_loss: Decimal
    take_profit: Decimal | None
    link_id: str


@dataclass(frozen=True, slots=True)
class ReduceOrder:
    """A partial close via a reduce-only limit order (an intermediate take-profit)."""

    asset: str
    position_side: Side  # side of the position being reduced
    qty: Decimal
    price: Decimal
    link_id: str


class ExchangeAdapter(Protocol):
    """What the execution layer needs from an exchange.

    Methods raise ``ExchangeError`` subclasses; network retries happen inside the adapter.
    """

    name: str

    async def get_instrument(self, asset: str) -> Instrument: ...
    async def get_price(self, asset: str) -> Decimal: ...
    async def get_balance(self, coin: str = "USDT") -> Balance: ...
    async def get_positions(self) -> list[PositionInfo]: ...
    async def set_leverage(self, asset: str, leverage: int) -> None: ...
    async def place_entry(self, order: EntryOrder) -> OrderAck: ...
    async def place_reduce_limit(self, order: ReduceOrder) -> OrderAck: ...
    async def get_order(self, asset: str, link_id: str) -> OrderState: ...
    async def amend_stop(self, asset: str, stop_loss: Decimal) -> None: ...
    async def cancel_all(self, asset: str) -> None: ...
    async def cancel_everything(self) -> None: ...
    async def close_position(self, asset: str) -> OrderAck | None: ...
    async def aclose(self) -> None: ...


def fmt(value: Decimal) -> str:
    """Decimal to a plain string without exponent or trailing zeros ('0.071', '62000')."""
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text
