"""Postgres writes for ingestion."""

from collections.abc import Sequence
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

from app.ingestion.sources.base import Candle, Market, Tick

UPSERT_CANDLE = """
INSERT INTO candles (coin, "interval", open_time, o, h, l, c, v, trades)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (coin, "interval", open_time) DO UPDATE
SET o = EXCLUDED.o, h = EXCLUDED.h, l = EXCLUDED.l, c = EXCLUDED.c,
    v = EXCLUDED.v, trades = EXCLUDED.trades
"""


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class Store:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def upsert_markets(self, markets: Sequence[Market]) -> None:
        rows = [(m.coin, m.asset_index, m.sz_decimals, m.max_leverage) for m in markets]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO markets (coin, asset_index, sz_decimals, max_leverage) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (coin) DO UPDATE SET "
                "asset_index = EXCLUDED.asset_index, sz_decimals = EXCLUDED.sz_decimals, "
                "max_leverage = EXCLUDED.max_leverage, updated_at = now()",
                rows,
            )

    async def upsert_candles(self, candles: Sequence[Candle]) -> None:
        if not candles:
            return
        rows = [
            (
                c.coin,
                c.interval,
                _ts(c.open_time_ms),
                c.open,
                c.high,
                c.low,
                c.close,
                c.volume,
                c.trades,
            )
            for c in candles
        ]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(UPSERT_CANDLE, rows)

    async def insert_quotes(self, ticks: Sequence[Tick]) -> None:
        if not ticks:
            return
        rows = [(t.symbol, _ts(t.timestamp_ms), t.bid, t.ask) for t in ticks]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO quotes (coin, ts, bid, ask) VALUES (%s, %s, %s, %s)", rows
            )

    async def latest_open_time_ms(self, coin: str, interval: str) -> int | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                'SELECT max(open_time) FROM candles WHERE coin = %s AND "interval" = %s',
                (coin, interval),
            )
            row = await cur.fetchone()
        return int(row[0].timestamp() * 1000) if row and row[0] else None

    async def set_quote_retention(self, days: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("SELECT remove_retention_policy('quotes', if_exists => true)")
            await conn.execute(
                "SELECT add_retention_policy('quotes', make_interval(days => %s))", (days,)
            )
