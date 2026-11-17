"""Latest quote per coin in Redis: hash `quote:<coin>` plus the `quotes` channel."""

import json
from typing import Any

from app.ingestion.sources.base import Tick

QUOTES_CHANNEL = "quotes"


def quote_key(coin: str) -> str:
    return f"quote:{coin}"


class QuotePublisher:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, tick: Tick) -> None:
        mapping = {"bid": tick.bid, "ask": tick.ask, "ts": tick.timestamp_ms}
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.hset(quote_key(tick.symbol), mapping=mapping)
            pipe.publish(QUOTES_CHANNEL, json.dumps({"coin": tick.symbol, **mapping}))
            await pipe.execute()
