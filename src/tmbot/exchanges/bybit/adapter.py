"""Bybit V5 adapter for USDT/USDC perpetuals (category=linear) on a Unified Trading Account.

Positions use one-way mode (positionIdx=0). The stop and the far take-profit are attached
to every entry in Full mode (whole position, market execution). Intermediate take-profits
are separate reduce-only limit orders, because Bybit does not allow TP/SL on reduce-only
orders.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from tmbot.core.models import Instrument, Side
from tmbot.exchanges.base import (
    Balance,
    EntryOrder,
    OrderAck,
    OrderState,
    OrderStatus,
    PositionInfo,
    ReduceOrder,
    RejectedError,
    fmt,
)
from tmbot.exchanges.bybit.client import BybitHttp, JsonDict

CATEGORY = "linear"
QUOTES = ("USDT", "USDC")  # order in which contracts are looked up for an asset
ONE_WAY = 0  # positionIdx in one-way mode
LEVERAGE_NOT_MODIFIED = 110043
DUPLICATE_LINK_ID = 110072

_STATUS = {
    "New": OrderStatus.NEW,
    "Untriggered": OrderStatus.NEW,
    "Triggered": OrderStatus.NEW,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "PartiallyFilledCanceled": OrderStatus.CANCELLED,
    "Deactivated": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED,
}


# --- response parsing -----------------------------------------------------------------


def dec(value: Any) -> Decimal:
    """Bybit sends numbers as strings, sometimes empty; empty or malformed becomes 0."""
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)


def dec_or_none(value: Any) -> Decimal | None:
    """Like ``dec``, but 0 (Bybit's "not set") becomes None."""
    number = dec(value)
    return number if number != 0 else None


def side_to_bybit(side: Side) -> str:
    return "Buy" if side is Side.LONG else "Sell"


def asset_from_symbol(symbol: str) -> str:
    for quote in (*QUOTES, "PERP"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol


def parse_instrument(asset: str, item: JsonDict) -> Instrument:
    price_filter = item.get("priceFilter", {})
    lot_filter = item.get("lotSizeFilter", {})
    leverage_filter = item.get("leverageFilter", {})
    try:
        return Instrument(
            asset=asset,
            exchange_symbol=item["symbol"],
            settle_coin=item.get("settleCoin", "USDT"),
            tick_size=dec(price_filter.get("tickSize")),
            qty_step=dec(lot_filter.get("qtyStep")),
            min_qty=dec(lot_filter.get("minOrderQty")),
            min_notional=dec(lot_filter.get("minNotionalValue")),
            max_leverage=int(dec(leverage_filter.get("maxLeverage")) or 1),
        )
    except (KeyError, ValueError) as exc:
        raise RejectedError(f"{asset}: некоректні параметри контракту від Bybit ({exc})") from exc


def parse_order(item: JsonDict) -> OrderState:
    avg_price = dec(item.get("avgPrice"))
    return OrderState(
        order_id=str(item.get("orderId", "")),
        link_id=str(item.get("orderLinkId", "")),
        status=_STATUS.get(str(item.get("orderStatus")), OrderStatus.UNKNOWN),
        qty=dec(item.get("qty")),
        filled_qty=dec(item.get("cumExecQty")),
        avg_price=avg_price if avg_price > 0 else None,
    )


def parse_position(item: JsonDict) -> PositionInfo | None:
    """None for an empty slot (Bybit lists symbols with size 0 when queried by symbol)."""
    size = dec(item.get("size"))
    raw_side = item.get("side")
    if size <= 0 or raw_side not in ("Buy", "Sell"):
        return None
    return PositionInfo(
        asset=asset_from_symbol(str(item["symbol"])),
        side=Side.LONG if raw_side == "Buy" else Side.SHORT,
        size=size,
        avg_price=dec(item.get("avgPrice")),
        mark_price=dec(item.get("markPrice")),
        leverage=dec(item.get("leverage")),
        unrealized_pnl=dec(item.get("unrealisedPnl")),
        liq_price=dec_or_none(item.get("liqPrice")),
        stop_loss=dec_or_none(item.get("stopLoss")),
        take_profit=dec_or_none(item.get("takeProfit")),
    )


def _time_tag() -> str:
    return format(int(time.time() * 1000), "x")


# --- adapter --------------------------------------------------------------------------


class BybitAdapter:
    name = "bybit"

    def __init__(self, http: BybitHttp) -> None:
        self.http = http
        self._instruments: dict[str, Instrument] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    # market data

    async def get_instrument(self, asset: str) -> Instrument:
        asset = asset.upper()
        if asset in self._instruments:
            return self._instruments[asset]
        for quote in QUOTES:
            try:
                res = await self.http.get(
                    "/v5/market/instruments-info",
                    {"category": CATEGORY, "symbol": f"{asset}{quote}"},
                    signed=False,
                )
            except RejectedError:
                continue  # Bybit answers an unknown symbol with a parameter error
            trading = [i for i in res.get("list", []) if i.get("status") == "Trading"]
            if trading:
                instrument = parse_instrument(asset, trading[0])
                self._instruments[asset] = instrument
                return instrument
        raise RejectedError(f"{asset}: немає активного perpetual-контракту на Bybit")

    async def get_price(self, asset: str) -> Decimal:
        res = await self.http.get("/v5/market/tickers", await self._params(asset), signed=False)
        items = res.get("list", [])
        price = dec(items[0].get("lastPrice")) if items else Decimal(0)
        if price <= 0:
            raise RejectedError(f"{asset}: Bybit не повернув ціну")
        return price

    # account

    async def get_balance(self, coin: str = "USDT") -> Balance:
        res = await self.http.get(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin}
        )
        accounts = res.get("list", [])
        if not accounts:
            raise RejectedError("Bybit: порожня відповідь wallet-balance (акаунт не UTA?)")
        account = accounts[0]
        rows: list[JsonDict] = account.get("coin", [])
        row = next((r for r in rows if r.get("coin") == coin), {})
        return Balance(
            coin=coin,
            realized=dec(row.get("walletBalance")),
            equity=dec(row.get("equity")),
            available=dec(account.get("totalAvailableBalance")),
        )

    async def get_positions(self) -> list[PositionInfo]:
        positions: list[PositionInfo] = []
        for coin in QUOTES:  # a settleCoin query returns only non-empty positions
            res = await self.http.get(
                "/v5/position/list", {"category": CATEGORY, "settleCoin": coin, "limit": 200}
            )
            positions += [p for p in map(parse_position, res.get("list", [])) if p]
        return positions

    # trading

    async def set_leverage(self, asset: str, leverage: int) -> None:
        body = await self._params(asset, buyLeverage=str(leverage), sellLeverage=str(leverage))
        try:
            await self.http.post("/v5/position/set-leverage", body)
        except RejectedError as exc:
            if exc.code != LEVERAGE_NOT_MODIFIED:
                raise

    async def place_entry(self, order: EntryOrder) -> OrderAck:
        body = await self._params(
            order.asset,
            side=side_to_bybit(order.side),
            orderType="Market" if order.price is None else "Limit",
            qty=fmt(order.qty),
            positionIdx=ONE_WAY,
            orderLinkId=order.link_id,
            tpslMode="Full",
            stopLoss=fmt(order.stop_loss),
        )
        if order.price is not None:
            body.update(price=fmt(order.price), timeInForce="GTC")
        if order.take_profit is not None:
            body["takeProfit"] = fmt(order.take_profit)
        return await self._create_order(order.asset, body)

    async def place_reduce_limit(self, order: ReduceOrder) -> OrderAck:
        body = await self._params(
            order.asset,
            side=side_to_bybit(order.position_side.opposite),
            orderType="Limit",
            qty=fmt(order.qty),
            price=fmt(order.price),
            timeInForce="GTC",
            reduceOnly=True,
            positionIdx=ONE_WAY,
            orderLinkId=order.link_id,
        )
        return await self._create_order(order.asset, body)

    async def get_order(self, asset: str, link_id: str) -> OrderState:
        params = await self._params(asset, orderLinkId=link_id)
        for path in ("/v5/order/realtime", "/v5/order/history"):
            items = (await self.http.get(path, params)).get("list", [])
            if items:
                return parse_order(items[0])
        raise RejectedError(f"ордер {link_id} не знайдено")

    async def amend_stop(self, asset: str, stop_loss: Decimal) -> None:
        body = await self._params(
            asset, tpslMode="Full", positionIdx=ONE_WAY, stopLoss=fmt(stop_loss)
        )
        await self.http.post("/v5/position/trading-stop", body)

    async def cancel_all(self, asset: str) -> None:
        await self.http.post("/v5/order/cancel-all", await self._params(asset))

    async def cancel_everything(self) -> None:
        for coin in QUOTES:
            await self.http.post("/v5/order/cancel-all", {"category": CATEGORY, "settleCoin": coin})

    async def close_position(self, asset: str, link_id: str | None = None) -> OrderAck | None:
        """Close the whole position at market. Returns None if there is no position."""
        res = await self.http.get("/v5/position/list", await self._params(asset))
        position = next((p for p in map(parse_position, res.get("list", [])) if p), None)
        if position is None:
            return None
        body = await self._params(
            asset,
            side=side_to_bybit(position.side.opposite),
            orderType="Market",
            qty=fmt(position.size),
            reduceOnly=True,
            positionIdx=ONE_WAY,
            orderLinkId=link_id or f"close-{asset}-{_time_tag()}",
        )
        return await self._create_order(asset, body)

    # helpers

    async def _params(self, asset: str, **extra: Any) -> JsonDict:
        """Base request parameters for an asset: category and exchange symbol."""
        instrument = await self.get_instrument(asset)
        return {"category": CATEGORY, "symbol": instrument.exchange_symbol, **extra}

    async def _create_order(self, asset: str, body: JsonDict) -> OrderAck:
        link_id = str(body["orderLinkId"])
        try:
            res = await self.http.post("/v5/order/create", body)
        except RejectedError as exc:
            # a retry after a timeout: the first attempt already created the order
            if exc.code == DUPLICATE_LINK_ID or "duplicate" in str(exc).lower():
                state = await self.get_order(asset, link_id)
                return OrderAck(order_id=state.order_id, link_id=link_id)
            raise
        return OrderAck(order_id=str(res.get("orderId", "")), link_id=link_id)
