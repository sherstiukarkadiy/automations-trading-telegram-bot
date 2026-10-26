"""Bybit V5 HTTP client: HMAC-SHA256 signing, retries, server time sync.

Signature: hex(HMAC_SHA256(secret, timestamp + api_key + recv_window + payload)), where
payload is the query string for GET and the JSON body for POST, exactly as sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from tmbot.exchanges.base import AuthError, ExchangeError, RejectedError, RetryableError
from tmbot.net import TLS_ERROR_HINT, is_tls_error, ssl_context

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"

RETRYABLE_CODES = {10006, 10016, 10429}  # rate limit, server error, system frequency
TIMESTAMP_CODE = 10002
AUTH_CODES = {10003, 10004, 10005, 10010}  # key, signature, permissions, IP
RETRYABLE_HTTP = {403, 429, 500, 502, 503, 504}  # Bybit answers an IP rate limit with 403

JsonDict = dict[str, Any]


class _Retry(Exception):
    """Internal: this attempt failed in a way another attempt may fix."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BybitHttp:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = True,
        recv_window: int = 5000,
        max_retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key = api_key
        self._secret = api_secret.encode()
        self.testnet = testnet
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self._recv_window = str(recv_window)
        self._attempts = max(max_retries, 0) + 1
        self._backoff = backoff
        self._clock = clock
        self._sleep = sleep
        self.time_offset_ms = 0
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport, verify=ssl_context()
        )

    def __repr__(self) -> str:  # never let the key or secret reach logs
        return f"BybitHttp(testnet={self.testnet}, key=***{self._key[-4:]})"

    async def aclose(self) -> None:
        await self._client.aclose()

    def sign(self, timestamp: str, payload: str) -> str:
        message = f"{timestamp}{self._key}{self._recv_window}{payload}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None, *, signed: bool = True
    ) -> JsonDict:
        return await self._request("GET", path, params or {}, signed)

    async def post(self, path: str, body: Mapping[str, Any]) -> JsonDict:
        return await self._request("POST", path, body, signed=True)

    async def sync_time(self) -> int:
        """Update the local clock offset relative to the Bybit server, in milliseconds."""
        try:
            data = (await self._client.get("/v5/market/time")).json()
        except httpx.TransportError as exc:
            if is_tls_error(exc):
                raise ExchangeError(TLS_ERROR_HINT) from exc
            raise RetryableError(f"Bybit час сервера: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise RetryableError("Bybit час сервера: відповідь не JSON") from exc
        self.time_offset_ms = int(data["time"]) - int(self._clock() * 1000)
        return self.time_offset_ms

    # --- request pipeline -----------------------------------------------------------

    async def _request(
        self, method: str, path: str, params: Mapping[str, Any], signed: bool
    ) -> JsonDict:
        clean = {k: v for k, v in params.items() if v is not None}
        reason = "невідома помилка"
        for attempt in range(self._attempts):
            if attempt:
                await self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                return await self._attempt(method, path, clean, signed)
            except _Retry as retry:
                reason = retry.reason
        raise RetryableError(f"Bybit {method} {path}: {reason} після {self._attempts} спроб")

    async def _attempt(self, method: str, path: str, params: JsonDict, signed: bool) -> JsonDict:
        try:
            resp = await self._send(method, path, params, signed)
        except httpx.TransportError as exc:
            if is_tls_error(exc):  # a retry cannot fix certificate verification
                raise ExchangeError(TLS_ERROR_HINT) from exc
            raise _Retry(f"мережа: {type(exc).__name__}") from exc
        if resp.status_code in RETRYABLE_HTTP:
            raise _Retry(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise _Retry(f"HTTP {resp.status_code}: не JSON") from exc
        return await self._unwrap(data)

    async def _unwrap(self, data: JsonDict) -> JsonDict:
        """Return ``result`` on success; otherwise retry or raise by ``retCode``."""
        code = int(data.get("retCode", -1))
        if code == 0:
            result: JsonDict = data.get("result") or {}
            return result
        message = f"{code}: {data.get('retMsg', '')}"
        if code == TIMESTAMP_CODE:
            await self.sync_time()
            raise _Retry(message)
        if code in RETRYABLE_CODES:
            raise _Retry(message)
        if code in AUTH_CODES:
            raise AuthError(f"Bybit {message}", code)
        raise RejectedError(f"Bybit {message}", code)

    async def _send(self, method: str, path: str, params: JsonDict, signed: bool) -> httpx.Response:
        headers: dict[str, str] = {}
        if method == "GET":
            query = urlencode(params)
            url = f"{path}?{query}" if query else path
            payload, content = query, None  # a GET with a body gets a 403 from Bybit
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url, content = path, payload.encode()
            headers["Content-Type"] = "application/json"

        if signed:
            timestamp = str(int(self._clock() * 1000) + self.time_offset_ms)
            headers |= {
                "X-BAPI-API-KEY": self._key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self._recv_window,
                "X-BAPI-SIGN": self.sign(timestamp, payload),
            }
        return await self._client.request(method, url, content=content, headers=headers)
