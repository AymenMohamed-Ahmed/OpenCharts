"""Gateway: the only backend service the terminal talks to."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.routers import health, orders
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
    yield
    await app.state.execution_client.aclose()


app = FastAPI(title="OpenBroker API", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(orders.router)
