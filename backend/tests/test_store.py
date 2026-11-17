import psycopg
from psycopg_pool import AsyncConnectionPool

from app.common.migrate import apply_migrations
from app.ingestion.pipelines.store import Store
from app.ingestion.sources.base import Candle, Market, Tick


async def test_store_round_trip_against_postgres(database_url):
    apply_migrations(database_url)
    async with AsyncConnectionPool(database_url, min_size=1, max_size=2, open=False) as pool:
        store = Store(pool)
        await store.upsert_markets([Market("BTC", 0, 5, 40)])
        await store.upsert_markets([Market("BTC", 0, 5, 50)])  # update, not duplicate

        t = 1_700_000_040_000
        await store.upsert_candles([Candle("BTC", "1m", t, 1, 2, 0.5, 1.5, 3, 1)])
        await store.upsert_candles(
            [Candle("BTC", "1m", t, 1, 2, 0.5, 1.9, 4, 2)]
        )  # same bar, final values
        assert await store.latest_open_time_ms("BTC", "1m") == t
        assert await store.latest_open_time_ms("BTC", "1h") is None

        await store.insert_quotes([Tick("BTC", 1.0, 2.0, t), Tick("BTC", 1.1, 2.1, t + 1000)])
        await store.set_quote_retention(3)
        await store.set_quote_retention(5)  # re-applying replaces the policy

    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT max_leverage FROM markets").fetchall() == [(50,)]
        assert conn.execute("SELECT c, v, trades FROM candles").fetchall() == [(1.9, 4.0, 2)]
        assert conn.execute("SELECT count(*) FROM quotes").fetchone() == (2,)
        policies = conn.execute(
            "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name = 'quotes'"
        ).fetchall()
        assert policies == [("5 days",)]
