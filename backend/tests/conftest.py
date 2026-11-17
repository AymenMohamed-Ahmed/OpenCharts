import pytest

from app.common import config


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "BROKER",
        "HL_NETWORK",
        "HL_ACCOUNT_ADDRESS",
        "HL_AGENT_PRIVATE_KEY",
        "LIVE_TRADING",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_AUTH_TOKEN", "test-api-token")
    monkeypatch.setenv("EXECUTION_INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MAX_ORDER_NOTIONAL_USD", "1000")
    monkeypatch.setenv("ALLOWED_SYMBOLS", "")
    for cached in (
        config.api_settings,
        config.agent_settings,
        config.ingestion_settings,
        config.execution_settings,
    ):
        cached.cache_clear()
    yield


def order(**overrides):
    body = {
        "client_order_id": "order-0001",
        "symbol": "BTC",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
        "reference_price": 50000,
    }
    body.update(overrides)
    return body


@pytest.fixture
def database_url():
    """A fresh, empty database on the TimescaleDB in TEST_DATABASE_URL (skipped without one).

    Each test gets its own database: dropping a schema under a live TimescaleDB
    background scheduler can deadlock.
    """
    import os
    import uuid

    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    yield make_conninfo(**{**conninfo_to_dict(url), "dbname": name})
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


class FakeRedis:
    """Just enough of redis.asyncio for the publisher and the quotes endpoint."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.published: list[tuple[str, str]] = []

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)

    async def aclose(self) -> None: ...


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._r = redis
        self._ops: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None: ...

    def hset(self, key, mapping):
        self._ops.append(
            lambda: self._r.hashes.setdefault(key, {}).update(
                {k: str(v) for k, v in mapping.items()}
            )
        )

    def publish(self, channel, message):
        self._ops.append(lambda: self._r.published.append((channel, message)))

    def hgetall(self, key):
        self._ops.append(
            lambda: {k.encode(): v.encode() for k, v in self._r.hashes.get(key, {}).items()}
        )

    async def execute(self):
        return [op() for op in self._ops]
