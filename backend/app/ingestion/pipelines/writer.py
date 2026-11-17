"""Batches candles and quotes into Postgres."""

import asyncio
import logging

from app.ingestion.pipelines.store import Store
from app.ingestion.sources.base import Candle, Tick

log = logging.getLogger(__name__)


class BatchWriter:
    """Buffers writes; flushes on size, or when `run()` ticks.

    Candle updates arrive many times per bar, so only the latest per
    (coin, interval, open time) is kept. Quotes are stored at most once per
    `quote_min_gap_ms` per coin (the newest wins); Redis still gets every tick.
    A failed flush keeps the buffer for the next attempt.
    """

    def __init__(
        self,
        store: Store,
        *,
        max_items: int = 500,
        flush_every_s: float = 1.0,
        quote_min_gap_ms: int = 1000,
        max_buffer: int = 50_000,
    ) -> None:
        self._store = store
        self._max_items = max_items
        self._flush_every_s = flush_every_s
        self._gap = quote_min_gap_ms
        self._max_buffer = max_buffer
        self._candles: dict[tuple[str, str, int], Candle] = {}
        self._quotes: list[Tick] = []
        self._last_quote_ms: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def add_candle(self, candle: Candle) -> None:
        self._candles[(candle.coin, candle.interval, candle.open_time_ms)] = candle

    def add_quote(self, tick: Tick) -> None:
        last = self._last_quote_ms.get(tick.symbol)
        if last is not None and tick.timestamp_ms - last < self._gap:
            return
        self._last_quote_ms[tick.symbol] = tick.timestamp_ms
        self._quotes.append(tick)
        if len(self._quotes) > self._max_buffer:  # database down for a long time
            del self._quotes[: len(self._quotes) - self._max_buffer]

    @property
    def pending(self) -> int:
        return len(self._candles) + len(self._quotes)

    async def flush(self) -> None:
        async with self._lock:
            candles, quotes = list(self._candles.values()), list(self._quotes)
            if not candles and not quotes:
                return
            await self._store.upsert_candles(candles)
            await self._store.insert_quotes(quotes)
            # Cleared only after both writes succeed; anything added meanwhile is kept.
            for c in candles:
                key = (c.coin, c.interval, c.open_time_ms)
                if self._candles.get(key) is c:
                    del self._candles[key]
            del self._quotes[: len(quotes)]

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_every_s)
            try:
                await self.flush()
            except Exception:
                log.exception("flush failed; will retry")
