"""Hyperliquid market data. Public endpoints, no credentials.

REST: `meta` (market list) and `candleSnapshot` (history, 5000 candles max per call).
Websocket: `bbo` and `candle` subscriptions, reconnecting with backoff.
Field mapping follows the terminal's src/hyperliquid/marketData.ts.
"""

import asyncio
import json
import logging
import math
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import websockets

from app.common.config import IngestionSettings
from app.ingestion.sources.base import (
    Candle,
    Market,
    MarketDataSource,
    Reconnected,
    StreamEvent,
    Tick,
)

log = logging.getLogger(__name__)

HL_MAX_CANDLES = 5000
INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 3_600_000,
    "2h": 2 * 3_600_000,
    "4h": 4 * 3_600_000,
    "8h": 8 * 3_600_000,
    "12h": 12 * 3_600_000,
    "1d": 86_400_000,
    "3d": 3 * 86_400_000,
    "1w": 7 * 86_400_000,
    "1M": 30 * 86_400_000,
}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_PING_EVERY_S = 30.0  # the server drops a connection after 60 s without a message

Sleep = Callable[[float], Awaitable[None]]


class HyperliquidError(RuntimeError):
    pass


def parse_candle(raw: dict[str, Any]) -> Candle | None:
    """Map a raw HL candle ({t, s, i, o, h, l, c, v, n}); None if it is malformed."""
    try:
        o, h, lo, c = (float(raw[k]) for k in ("o", "h", "l", "c"))
        t = int(raw["t"])
        coin, interval = str(raw["s"]), str(raw["i"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (o, h, lo, c)):
        return None
    try:
        v = float(raw.get("v", 0))
    except (TypeError, ValueError):
        v = 0.0
    if not math.isfinite(v):
        v = 0.0
    n = raw.get("n", 0)
    return Candle(coin, interval, t, o, h, lo, c, v, n if isinstance(n, int) else 0)


class HyperliquidClient:
    """REST client for /info with retry and backoff on 429, 5xx and transport errors."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 5,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=15.0)
        self._owns_client = client is None
        self._url = f"{base_url}/info"
        self._max_attempts = max_attempts
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _info(self, body: dict[str, Any]) -> Any:
        delay = 0.5
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._client.post(self._url, json=body)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise HyperliquidError(f"info request failed: {exc!r}") from exc
            else:
                if resp.status_code not in _RETRY_STATUS:
                    if resp.status_code >= 400:
                        raise HyperliquidError(f"info {resp.status_code}: {resp.text[:200]}")
                    return resp.json()
                if attempt == self._max_attempts:
                    raise HyperliquidError(f"info {resp.status_code} after {attempt} attempts")
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 30.0))
            await self._sleep(delay * (0.5 + random.random()))  # noqa: S311 - jitter only
            delay = min(delay * 2, 30.0)
        raise AssertionError("unreachable")

    async def meta(self) -> list[Market]:
        data = await self._info({"type": "meta"})
        markets: list[Market] = []
        for index, asset in enumerate(data.get("universe", [])):
            if asset.get("isDelisted"):
                continue
            markets.append(
                Market(
                    coin=asset["name"],
                    asset_index=index,
                    sz_decimals=int(asset["szDecimals"]),
                    max_leverage=int(asset["maxLeverage"]),
                )
            )
        return markets

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        """Candles with open time in [start_ms, end_ms], oldest first, paged past 5000."""
        step = INTERVAL_MS.get(interval)
        if step is None:
            raise ValueError(f"unsupported interval {interval!r}")
        out: dict[int, Candle] = {}
        cursor = start_ms
        while cursor <= end_ms:
            req = {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms}
            rows = await self._info({"type": "candleSnapshot", "req": req})
            page = [c for r in rows if (c := parse_candle(r)) and c.open_time_ms >= cursor]
            if not page:
                break
            for c in page:
                out[c.open_time_ms] = c
            last = max(c.open_time_ms for c in page)
            if len(rows) < HL_MAX_CANDLES or last + step > end_ms:
                break
            cursor = last + step
        return [out[t] for t in sorted(out)]


class HyperliquidSource(MarketDataSource):
    name = "hyperliquid"

    def __init__(
        self,
        settings: IngestionSettings,
        *,
        ws_url: str | None = None,
        sleep: Sleep = asyncio.sleep,
        max_backoff_s: float = 30.0,
    ) -> None:
        self.base_url = settings.hl_api_url
        self.rest_url = f"{self.base_url}/info"
        self.ws_url = ws_url or settings.hl_api_url.replace("https://", "wss://") + "/ws"
        self._sleep = sleep
        self._max_backoff_s = max_backoff_s

    async def stream(self, coins: list[str], intervals: list[str]) -> AsyncIterator[StreamEvent]:
        """Yield ticks and candle updates forever; `Reconnected` after each (re)subscribe."""
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, open_timeout=15) as ws:
                    await self._subscribe(ws, coins, intervals)
                    backoff = 1.0
                    yield Reconnected()
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for message in ws:
                            for event in self._decode(message, set(coins), set(intervals)):
                                yield event
                    finally:
                        pinger.cancel()
                log.warning("hyperliquid websocket closed; reconnecting")
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                log.warning("hyperliquid websocket error: %r; retrying in %.0fs", exc, backoff)
            await self._sleep(backoff * (0.5 + random.random()))  # noqa: S311 - jitter only
            backoff = min(backoff * 2, self._max_backoff_s)

    @staticmethod
    async def _subscribe(ws: Any, coins: list[str], intervals: list[str]) -> None:
        subs: list[dict[str, str]] = [{"type": "bbo", "coin": c} for c in coins]
        subs += [{"type": "candle", "coin": c, "interval": i} for c in coins for i in intervals]
        for sub in subs:
            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    @staticmethod
    async def _ping(ws: Any) -> None:
        while True:
            await asyncio.sleep(_PING_EVERY_S)
            await ws.send(json.dumps({"method": "ping"}))

    @staticmethod
    def _decode(message: str | bytes, coins: set[str], intervals: set[str]) -> list[StreamEvent]:
        try:
            frame = json.loads(message)
            channel, data = frame["channel"], frame["data"]
        except (ValueError, KeyError, TypeError):
            return []
        if channel == "bbo" and isinstance(data, dict):
            tick = _tick(data)
            return [tick] if tick and tick.symbol in coins else []
        if channel == "candle":
            # The documented payload is a list; the live feed sends a bare object.
            rows = data if isinstance(data, list) else [data]
            candles = (parse_candle(r) for r in rows if isinstance(r, dict))
            return [c for c in candles if c and c.coin in coins and c.interval in intervals]
        return []


def _tick(data: dict[str, Any]) -> Tick | None:
    try:
        bbo = data["bbo"]
        bid, ask = float(bbo[0]["px"]), float(bbo[1]["px"])
        coin, ts = str(data["coin"]), int(data["time"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask <= 0:
        return None
    return Tick(coin, min(bid, ask), max(bid, ask), ts)
