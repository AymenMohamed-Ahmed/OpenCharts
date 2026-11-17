"""Ingestion loop: markets, backfill, live stream, reconnect gap-fill."""

import asyncio
import logging
import time
from typing import Any

from app.common.config import IngestionSettings
from app.ingestion.pipelines.redis_out import QuotePublisher
from app.ingestion.pipelines.store import Store
from app.ingestion.pipelines.writer import BatchWriter
from app.ingestion.sources.base import Candle, Reconnected, Tick
from app.ingestion.sources.hyperliquid import INTERVAL_MS, HyperliquidClient, HyperliquidSource

log = logging.getLogger(__name__)
MARKET_REFRESH_S = 600.0


class IngestionService:
    def __init__(
        self,
        settings: IngestionSettings,
        client: HyperliquidClient,
        source: HyperliquidSource,
        store: Store,
        writer: BatchWriter,
        publisher: QuotePublisher,
        *,
        now_ms: Any = lambda: int(time.time() * 1000),
    ) -> None:
        self._s = settings
        self._client = client
        self._source = source
        self._store = store
        self._writer = writer
        self._publisher = publisher
        self._now_ms = now_ms
        self.coins: list[str] = []

    async def refresh_markets(self) -> None:
        markets = await self._client.meta()
        await self._store.upsert_markets(markets)
        known = {m.coin for m in markets}
        wanted = self._s.coins
        missing = [c for c in wanted if c not in known]
        if missing:
            log.warning("INGEST_COINS not listed on %s, skipped: %s", self._s.hl_network, missing)
        self.coins = [c for c in wanted if c in known]

    async def backfill(self, coin: str, interval: str) -> int:
        """Fetch candles from the last stored bar (or the backfill window) up to now."""
        now = self._now_ms()
        floor = now - self._s.ingest_backfill_days * 86_400_000
        last = await self._store.latest_open_time_ms(coin, interval)
        # Start at the last stored bar so a bar that was still forming gets its final values.
        start = max(floor, last) if last is not None else floor
        candles = await self._client.candles(coin, interval, start, now)
        await self._store.upsert_candles(candles)
        return len(candles)

    async def backfill_all(self) -> None:
        for coin in self.coins:
            for interval in self._s.intervals:
                try:
                    n = await self.backfill(coin, interval)
                    log.info("backfilled %s %s: %d candles", coin, interval, n)
                except Exception:
                    log.exception("backfill failed for %s %s", coin, interval)

    async def handle(self, event: Candle | Tick | Reconnected) -> None:
        if isinstance(event, Tick):
            self._writer.add_quote(event)
            try:
                await self._publisher.publish(event)
            except Exception as exc:
                log.warning("redis publish failed: %r", exc)
        elif isinstance(event, Candle):
            self._writer.add_candle(event)
        else:
            await self.backfill_all()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(MARKET_REFRESH_S)
            try:
                await self.refresh_markets()
            except Exception:
                log.warning("market refresh failed", exc_info=True)

    async def run(self) -> None:
        for interval in self._s.intervals:
            if interval not in INTERVAL_MS:
                raise ValueError(f"INGEST_INTERVALS has unsupported interval {interval!r}")
        await self._store.set_quote_retention(self._s.quote_retention_days)
        while True:  # the exchange may be briefly unreachable at start
            try:
                await self.refresh_markets()
                break
            except Exception as exc:
                log.warning("market list unavailable (%r); retrying in 5s", exc)
                await asyncio.sleep(5)
        if not self.coins:
            raise RuntimeError("none of INGEST_COINS is listed; nothing to ingest")
        tasks = [asyncio.create_task(self._writer.run()), asyncio.create_task(self._refresh_loop())]
        try:
            # The first Reconnected event triggers the initial backfill, after the
            # live stream is subscribed, so no bar falls between the two.
            async for event in self._source.stream(self.coins, self._s.intervals):
                await self.handle(event)
        finally:
            for t in tasks:
                t.cancel()
            await self._writer.flush()
