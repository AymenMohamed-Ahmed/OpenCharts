"""Gateway: the only backend service the terminal talks to."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from app.api.routers import health, market, orders
from app.common.config import api_settings
from app.common.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = api_settings()
    configure_logging(settings.log_level)
    # trust_env=False: calls to execution stay on the internal network and
    # must never be routed through a proxy picked up from the environment.
    app.state.execution_client = httpx.AsyncClient(
        base_url=settings.execution_url, timeout=10.0, trust_env=False
    )
    # Market data is read from Postgres and Redis only; the gateway never calls Hyperliquid.
    app.state.db_pool = None
    app.state.redis = None
    if settings.database_url:
        app.state.db_pool = AsyncConnectionPool(
            settings.database_url.get_secret_value(), min_size=1, max_size=4, open=False
        )
        await app.state.db_pool.open()
    if settings.redis_url:
        app.state.redis = Redis.from_url(settings.redis_url.get_secret_value())
    yield
    await app.state.execution_client.aclose()
    if app.state.db_pool:
        await app.state.db_pool.close()
    if app.state.redis:
        await app.state.redis.aclose()


app = FastAPI(title="OpenBroker API", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(orders.router)
app.include_router(market.router)
