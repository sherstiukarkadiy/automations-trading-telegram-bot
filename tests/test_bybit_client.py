import hashlib
import hmac
import json

import httpx
import pytest
import respx

from tmbot.exchanges.base import AuthError, ExchangeError, RejectedError, RetryableError
from tmbot.exchanges.bybit.client import TESTNET_URL, BybitHttp

KEY, SECRET = "testkey", "testsecret"
NOW = 1_700_000_000.0


async def no_sleep(_: float) -> None:
    return None


def make_http(**kw: object) -> BybitHttp:
    return BybitHttp(KEY, SECRET, clock=lambda: NOW, sleep=no_sleep, **kw)  # type: ignore[arg-type]


def ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result, "time": 1})


def expected_sign(ts: str, payload: str) -> str:
    msg = f"{ts}{KEY}5000{payload}".encode()
    return hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()


@respx.mock
async def test_get_is_signed_over_exact_query() -> None:
    route = respx.get(f"{TESTNET_URL}/v5/account/wallet-balance").mock(return_value=ok({"x": 1}))
    http = make_http()
    res = await http.get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
    assert res == {"x": 1}
    req = route.calls.last.request
    assert req.url.query == b"accountType=UNIFIED&coin=USDT"
    ts = req.headers["X-BAPI-TIMESTAMP"]
    assert ts == str(int(NOW * 1000))
    assert req.headers["X-BAPI-SIGN"] == expected_sign(ts, "accountType=UNIFIED&coin=USDT")
    assert req.headers["X-BAPI-API-KEY"] == KEY
    assert not req.content  # a GET with a body gets a 403 from Bybit


@respx.mock
async def test_post_is_signed_over_exact_body() -> None:
    route = respx.post(f"{TESTNET_URL}/v5/order/create").mock(return_value=ok({"orderId": "1"}))
    http = make_http()
    await http.post("/v5/order/create", {"symbol": "BTCUSDT", "qty": "0.01", "price": None})
    req = route.calls.last.request
    body = req.content.decode()
    assert json.loads(body) == {"symbol": "BTCUSDT", "qty": "0.01"}  # None values dropped
    assert req.headers["X-BAPI-SIGN"] == expected_sign(req.headers["X-BAPI-TIMESTAMP"], body)


@respx.mock
async def test_rate_limit_is_retried() -> None:
    route = respx.get(f"{TESTNET_URL}/v5/position/list").mock(
        side_effect=[
            httpx.Response(200, json={"retCode": 10006, "retMsg": "Too many visits"}),
            httpx.Response(503),
            ok({"list": []}),
        ]
    )
    assert await make_http().get("/v5/position/list", {"category": "linear"}) == {"list": []}
    assert route.call_count == 3


@respx.mock
async def test_network_error_exhausts_retries() -> None:
    respx.get(f"{TESTNET_URL}/v5/position/list").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RetryableError):
        await make_http(max_retries=2).get("/v5/position/list")


@respx.mock
async def test_timestamp_error_resyncs_clock() -> None:
    respx.get(f"{TESTNET_URL}/v5/market/time").mock(
        return_value=httpx.Response(200, json={"retCode": 0, "time": int(NOW * 1000) + 2500})
    )
    route = respx.get(f"{TESTNET_URL}/v5/position/list").mock(
        side_effect=[
            httpx.Response(200, json={"retCode": 10002, "retMsg": "timestamp"}),
            ok({}),
        ]
    )
    http = make_http()
    await http.get("/v5/position/list")
    assert http.time_offset_ms == 2500
    assert route.calls.last.request.headers["X-BAPI-TIMESTAMP"] == str(int(NOW * 1000) + 2500)


@respx.mock
async def test_auth_and_business_errors_not_retried() -> None:
    route = respx.get(f"{TESTNET_URL}/v5/user/query-api").mock(
        return_value=httpx.Response(200, json={"retCode": 10010, "retMsg": "unmatched IP"})
    )
    with pytest.raises(AuthError):
        await make_http().get("/v5/user/query-api")
    assert route.call_count == 1

    respx.post(f"{TESTNET_URL}/v5/order/create").mock(
        return_value=httpx.Response(200, json={"retCode": 110007, "retMsg": "insufficient"})
    )
    with pytest.raises(RejectedError) as err:
        await make_http().post("/v5/order/create", {})
    assert err.value.code == 110007


def test_repr_hides_secrets() -> None:
    text = repr(BybitHttp("abcdefgh1234", "topsecret"))
    assert "topsecret" not in text and "abcdefgh" not in text


@respx.mock
async def test_tls_error_is_not_retried_and_explained() -> None:
    route = respx.get(f"{TESTNET_URL}/v5/position/list").mock(
        side_effect=httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain"
        )
    )
    with pytest.raises(ExchangeError) as err:
        await make_http().get("/v5/position/list")
    assert "проксі" in str(err.value)
    assert route.call_count == 1


@respx.mock
async def test_sync_time_tls_error_wrapped() -> None:
    respx.get(f"{TESTNET_URL}/v5/market/time").mock(
        side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] x")
    )
    with pytest.raises(ExchangeError):
        await make_http().sync_time()
