from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from app.api.main import app as api_app
from app.common.migrate import apply_migrations
from tests.conftest import FakeRedis

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def client():
    with TestClient(api_app) as c:
        c.app.state.redis = FakeRedis()
        yield c


@pytest.mark.parametrize(
    "path",
    [
        "/v1/market/symbols",
        "/v1/market/candles?coin=BTC&interval=1m",
        "/v1/market/quotes?coins=BTC",
    ],
)
def test_reads_need_the_api_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_no_database_is_a_503_not_a_crash(client):
    assert client.get("/v1/market/symbols", headers=AUTH).status_code == 503


@pytest.mark.parametrize(
    "query",
    [
        "coin=BTC&interval=7m",
        "coin=BT%20C&interval=1m",
        "coin=BTC&interval=1m&limit=0",
        "coin=BTC&interval=1m&limit=5001",
    ],
)
def test_candle_params_are_validated(client, query):
    assert client.get(f"/v1/market/candles?{query}", headers=AUTH).status_code == 422


def test_quotes_read_redis_and_omit_unknown_coins(client):
    client.app.state.redis.hashes["quote:BTC"] = {"bid": "100.5", "ask": "101", "ts": "1234"}
    r = client.get("/v1/market/quotes?coins=BTC,ETH,BTC", headers=AUTH)
    assert r.json() == [{"coin": "BTC", "bid": 100.5, "ask": 101.0, "time": 1234}]
    assert client.get("/v1/market/quotes?coins=", headers=AUTH).status_code == 422
    assert (
        client.get(
            "/v1/market/quotes?coins=" + ",".join(f"C{i}" for i in range(51)), headers=AUTH
        ).status_code
        == 422
    )


@pytest.fixture
def db_client(database_url):
    apply_migrations(database_url)
    with TestClient(api_app) as c:
        pool = AsyncConnectionPool(database_url, min_size=1, max_size=2, open=False)
        c.portal.call(pool.open)
        c.app.state.db_pool = pool
        c.app.state.redis = FakeRedis()
        yield c, database_url
        c.portal.call(pool.close)


def ms(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def test_symbols_and_candles_from_postgres(db_client):
    import psycopg

    client, url = db_client
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO markets VALUES ('ETH', 1, 4, 25, now()), ('BTC', 0, 5, 40, now())"
        )
        for i in range(5):
            t = datetime(2026, 1, 1, 0, i, tzinfo=UTC)
            conn.execute(
                "INSERT INTO candles VALUES ('BTC','1m',%s,1,3,0.5,%s,10,2)", (t, float(i))
            )
        conn.execute(
            "INSERT INTO candles VALUES ('BTC','1h',%s,1,3,0.5,9,10,2)",
            (datetime(2026, 1, 1, tzinfo=UTC),),
        )
        conn.execute(
            "INSERT INTO candles VALUES ('ETH','1m',%s,1,3,0.5,9,10,2)",
            (datetime(2026, 1, 1, tzinfo=UTC),),
        )

    symbols = client.get("/v1/market/symbols", headers=AUTH).json()
    assert [
        (s["coin"], s["asset_index"], s["sz_decimals"], s["max_leverage"]) for s in symbols
    ] == [("BTC", 0, 5, 40), ("ETH", 1, 4, 25)]

    def get(**params):
        r = client.get(
            "/v1/market/candles", params={"coin": "BTC", "interval": "1m", **params}, headers=AUTH
        )
        assert r.status_code == 200, r.text
        return r.json()

    all_ = get()
    assert [c["close"] for c in all_] == [0, 1, 2, 3, 4]  # one coin, one interval, oldest first
    assert all_[0] == {
        "time": ms(2026, 1, 1),
        "open": 1,
        "high": 3,
        "low": 0.5,
        "close": 0,
        "volume": 10,
        "trades": 2,
    }
    assert [c["close"] for c in get(limit=2)] == [3, 4]  # newest two, still ascending
    window = get(**{"from": ms(2026, 1, 1, 0, 1), "to": ms(2026, 1, 1, 0, 3)})
    assert [c["close"] for c in window] == [1, 2, 3]  # inclusive bounds
    assert get(coin="NOPE") == []
