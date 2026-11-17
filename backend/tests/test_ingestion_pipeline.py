import json

import pytest

from app.common.config import IngestionSettings
from app.ingestion.pipelines.redis_out import QUOTES_CHANNEL, QuotePublisher
from app.ingestion.pipelines.writer import BatchWriter
from app.ingestion.service import IngestionService
from app.ingestion.sources.base import Candle, Market, Reconnected, Tick
from tests.conftest import FakeRedis


def candle(t, close=1.0, coin="BTC", interval="1m"):
    return Candle(coin, interval, t, 1, 2, 0.5, close, 3, 1)


class FakeStore:
    def __init__(self):
        self.candles: list[Candle] = []
        self.quotes: list[Tick] = []
        self.markets: list[Market] = []
        self.latest: dict[tuple[str, str], int] = {}
        self.retention = None
        self.fail = False

    async def upsert_candles(self, candles):
        if self.fail:
            raise RuntimeError("db down")
        self.candles += candles

    async def insert_quotes(self, ticks):
        if self.fail:
            raise RuntimeError("db down")
        self.quotes += ticks

    async def upsert_markets(self, markets):
        self.markets = list(markets)

    async def latest_open_time_ms(self, coin, interval):
        return self.latest.get((coin, interval))

    async def set_quote_retention(self, days):
        self.retention = days


async def test_writer_keeps_latest_candle_and_throttles_quotes():
    store = FakeStore()
    w = BatchWriter(store, quote_min_gap_ms=1000)
    w.add_candle(candle(60_000, close=1))
    w.add_candle(candle(60_000, close=9))
    for ts in (0, 400, 999, 1000, 1500):
        w.add_quote(Tick("BTC", 1, 2, ts))
    w.add_quote(Tick("ETH", 1, 2, 5))
    await w.flush()
    assert [c.close for c in store.candles] == [9]
    assert [(q.symbol, q.timestamp_ms) for q in store.quotes] == [
        ("BTC", 0),
        ("BTC", 1000),
        ("ETH", 5),
    ]
    assert w.pending == 0


async def test_writer_keeps_the_buffer_when_the_database_fails():
    store = FakeStore()
    w = BatchWriter(store)
    w.add_candle(candle(60_000))
    w.add_quote(Tick("BTC", 1, 2, 0))
    store.fail = True
    with pytest.raises(RuntimeError):
        await w.flush()
    assert w.pending == 2
    store.fail = False
    await w.flush()
    assert len(store.candles) == 1 and len(store.quotes) == 1 and w.pending == 0


async def test_publisher_writes_hash_and_channel():
    redis = FakeRedis()
    await QuotePublisher(redis).publish(Tick("BTC", 100.5, 101.0, 1234))
    assert redis.hashes["quote:BTC"] == {"bid": "100.5", "ask": "101.0", "ts": "1234"}
    ((channel, message),) = redis.published
    assert channel == QUOTES_CHANNEL
    assert json.loads(message) == {"coin": "BTC", "bid": 100.5, "ask": 101.0, "ts": 1234}


class FakeClient:
    def __init__(self):
        self.calls = []

    async def meta(self):
        return [Market("BTC", 0, 5, 40), Market("ETH", 1, 4, 25)]

    async def candles(self, coin, interval, start, end):
        self.calls.append((coin, interval, start, end))
        return [candle(start, coin=coin, interval=interval)]


def make_service(store, *, coins="BTC,DOGE", **kw):
    settings = IngestionSettings(ingest_coins=coins, ingest_intervals="1m", ingest_backfill_days=1)
    writer = BatchWriter(store)
    redis = FakeRedis()
    client = FakeClient()
    svc = IngestionService(
        settings, client, None, store, writer, QuotePublisher(redis), now_ms=lambda: 10 * 86_400_000
    )
    return svc, client, writer, redis


async def test_unknown_coins_are_skipped():
    store = FakeStore()
    svc, *_ = make_service(store)
    await svc.refresh_markets()
    assert svc.coins == ["BTC"]
    assert [m.coin for m in store.markets] == ["BTC", "ETH"]


async def test_backfill_starts_at_window_or_last_stored_bar():
    store = FakeStore()
    svc, client, *_ = make_service(store)
    await svc.refresh_markets()
    now = 10 * 86_400_000
    await svc.backfill("BTC", "1m")
    assert client.calls[-1][2] == now - 86_400_000  # nothing stored: the window
    store.latest[("BTC", "1m")] = now - 5000
    await svc.backfill("BTC", "1m")
    assert client.calls[-1][2] == now - 5000  # gap only
    store.latest[("BTC", "1m")] = 1
    await svc.backfill("BTC", "1m")
    assert client.calls[-1][2] == now - 86_400_000  # never older than the window


async def test_events_route_to_writer_redis_and_backfill():
    store = FakeStore()
    svc, client, writer, redis = make_service(store)
    await svc.refresh_markets()
    await svc.handle(Tick("BTC", 1, 2, 7))
    await svc.handle(candle(60_000))
    assert "quote:BTC" in redis.hashes and writer.pending == 2
    await svc.handle(Reconnected())
    assert [c[:2] for c in client.calls] == [("BTC", "1m")]
    await writer.flush()
    assert store.quotes and store.candles
