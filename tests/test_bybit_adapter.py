import json
from decimal import Decimal as D

import httpx
import pytest
import respx

from tmbot.core.models import Side
from tmbot.exchanges.base import EntryOrder, ReduceOrder, RejectedError
from tmbot.exchanges.bybit.adapter import BybitAdapter, asset_from_symbol
from tmbot.exchanges.bybit.client import TESTNET_URL, BybitHttp

U = TESTNET_URL


async def no_sleep(_: float) -> None:
    return None


def ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def err(code: int, msg: str = "error") -> httpx.Response:
    return httpx.Response(200, json={"retCode": code, "retMsg": msg})


BTC_INFO = {
    "list": [
        {
            "symbol": "BTCUSDT",
            "status": "Trading",
            "settleCoin": "USDT",
            "priceFilter": {"tickSize": "0.10", "minPrice": "0.10"},
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
            "leverageFilter": {"maxLeverage": "100.00", "minLeverage": "1"},
        }
    ]
}


@pytest.fixture
def adapter() -> BybitAdapter:
    return BybitAdapter(BybitHttp("k", "s", sleep=no_sleep))


def body_of(route: respx.Route) -> dict[str, object]:
    data: dict[str, object] = json.loads(route.calls.last.request.content)
    return data


@respx.mock
async def test_instrument_parsed_and_cached(adapter: BybitAdapter) -> None:
    route = respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    inst = await adapter.get_instrument("btc")
    await adapter.get_instrument("BTC")
    assert route.call_count == 1
    assert inst.asset == "BTC" and inst.exchange_symbol == "BTCUSDT"
    assert inst.tick_size == D("0.10") and inst.qty_step == D("0.001")
    assert inst.max_leverage == 100 and inst.min_notional == D(5)


@respx.mock
async def test_instrument_falls_back_to_usdc(adapter: BybitAdapter) -> None:
    usdc = {"list": [{**BTC_INFO["list"][0], "symbol": "XYZUSDC", "settleCoin": "USDC"}]}
    respx.get(f"{U}/v5/market/instruments-info", params={"symbol": "XYZUSDT"}).mock(
        return_value=err(10001, "symbol invalid")
    )
    respx.get(f"{U}/v5/market/instruments-info", params={"symbol": "XYZUSDC"}).mock(
        return_value=ok(usdc)
    )
    inst = await adapter.get_instrument("XYZ")
    assert inst.exchange_symbol == "XYZUSDC" and inst.settle_coin == "USDC"


@respx.mock
async def test_unknown_asset_raises(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok({"list": []}))
    with pytest.raises(RejectedError):
        await adapter.get_instrument("NOPE")


@respx.mock
async def test_balance_uses_wallet_balance_for_realized(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/account/wallet-balance").mock(
        return_value=ok(
            {
                "list": [
                    {
                        "totalAvailableBalance": "900.5",
                        "coin": [
                            {"coin": "USDT", "walletBalance": "1000", "equity": "1012.3"},
                        ],
                    }
                ]
            }
        )
    )
    bal = await adapter.get_balance()
    assert bal.realized == D(1000) and bal.equity == D("1012.3") and bal.available == D("900.5")


@respx.mock
async def test_positions_skip_empty_and_map_side(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/position/list", params={"settleCoin": "USDT"}).mock(
        return_value=ok(
            {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "size": "0.02",
                        "avgPrice": "61000",
                        "markPrice": "60900",
                        "leverage": "10",
                        "unrealisedPnl": "2",
                        "liqPrice": "",
                        "stopLoss": "62000",
                        "takeProfit": "0",
                    },
                    {"symbol": "ETHUSDT", "side": "", "size": "0"},
                ]
            }
        )
    )
    respx.get(f"{U}/v5/position/list", params={"settleCoin": "USDC"}).mock(
        return_value=ok({"list": []})
    )
    [pos] = await adapter.get_positions()
    assert pos.asset == "BTC" and pos.side is Side.SHORT and pos.size == D("0.02")
    assert pos.liq_price is None and pos.take_profit is None and pos.stop_loss == D(62000)


@respx.mock
async def test_limit_entry_body(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    route = respx.post(f"{U}/v5/order/create").mock(return_value=ok({"orderId": "abc"}))
    ack = await adapter.place_entry(
        EntryOrder("BTC", Side.LONG, D("0.071"), D("61200.0"), D("60100"), D("65500"), "tm1-e1")
    )
    assert ack.order_id == "abc"
    assert body_of(route) == {
        "category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Limit",
        "qty": "0.071", "positionIdx": 0, "orderLinkId": "tm1-e1", "tpslMode": "Full",
        "stopLoss": "60100", "price": "61200", "timeInForce": "GTC", "takeProfit": "65500",
    }  # fmt: skip


@respx.mock
async def test_market_entry_has_no_price(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    route = respx.post(f"{U}/v5/order/create").mock(return_value=ok({"orderId": "m"}))
    await adapter.place_entry(EntryOrder("BTC", Side.SHORT, D("0.01"), None, D(62000), None, "x"))
    body = body_of(route)
    assert body["orderType"] == "Market" and body["side"] == "Sell"
    assert "price" not in body and "takeProfit" not in body


@respx.mock
async def test_reduce_limit_is_opposite_side_without_tpsl(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    route = respx.post(f"{U}/v5/order/create").mock(return_value=ok({"orderId": "t"}))
    await adapter.place_reduce_limit(ReduceOrder("BTC", Side.LONG, D("0.047"), D(63000), "tp1"))
    body = body_of(route)
    assert body["side"] == "Sell" and body["reduceOnly"] is True
    assert "stopLoss" not in body and "takeProfit" not in body


@respx.mock
async def test_duplicate_link_id_returns_existing_order(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    respx.post(f"{U}/v5/order/create").mock(return_value=err(110072, "OrderLinkedID is duplicate"))
    respx.get(f"{U}/v5/order/realtime").mock(
        return_value=ok({"list": [{"orderId": "orig", "orderLinkId": "x", "orderStatus": "New"}]})
    )
    ack = await adapter.place_entry(EntryOrder("BTC", Side.LONG, D(1), D(1), D("0.5"), None, "x"))
    assert ack.order_id == "orig"


@respx.mock
async def test_leverage_not_modified_is_ok(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    respx.post(f"{U}/v5/position/set-leverage").mock(return_value=err(110043, "not modified"))
    await adapter.set_leverage("BTC", 10)


@respx.mock
async def test_get_order_falls_back_to_history(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    respx.get(f"{U}/v5/order/realtime").mock(return_value=ok({"list": []}))
    respx.get(f"{U}/v5/order/history").mock(
        return_value=ok(
            {
                "list": [
                    {
                        "orderId": "1",
                        "orderLinkId": "e0",
                        "orderStatus": "Filled",
                        "qty": "0.071",
                        "cumExecQty": "0.071",
                        "avgPrice": "61805.5",
                    }
                ]
            }
        )
    )
    state = await adapter.get_order("BTC", "e0")
    assert state.status == "filled" and state.filled_qty == D("0.071")
    assert state.avg_price == D("61805.5")


@respx.mock
async def test_close_position_market_reduce_only(adapter: BybitAdapter) -> None:
    respx.get(f"{U}/v5/market/instruments-info").mock(return_value=ok(BTC_INFO))
    respx.get(f"{U}/v5/position/list").mock(
        return_value=ok({"list": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.142"}]})
    )
    route = respx.post(f"{U}/v5/order/create").mock(return_value=ok({"orderId": "c"}))
    ack = await adapter.close_position("BTC")
    assert ack is not None
    body = body_of(route)
    assert body["side"] == "Sell" and body["qty"] == "0.142" and body["reduceOnly"] is True


def test_asset_from_symbol() -> None:
    assert asset_from_symbol("BTCUSDT") == "BTC"
    assert asset_from_symbol("1000PEPEUSDT") == "1000PEPE"
    assert asset_from_symbol("ETHUSDC") == "ETH"
