"""Gateway: the only backend service the terminal talks to."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.execution_client import fetch_account
from app.api.risk.controls import RiskControls
from app.api.routers import admin, health, orders
from app.common.config import ApiSettings, api_settings
from app.common.logging import configure_logging

log = logging.getLogger(__name__)
SNAPSHOT_INTERVAL_S = 60.0


class _AppRequest:
    """The parts of a Request the execution helpers use, for background work."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


async def snapshot_once(app: FastAPI, settings: ApiSettings) -> None:
    """Write today's starting equity if it is not there yet (00:00 UTC)."""
    risk: RiskControls | None = app.state.risk_controls
    if risk is None or await risk.day_baseline() is not None:
        return
    account = await fetch_account(_AppRequest(app), settings)  # type: ignore[arg-type]
    baseline = await risk.snapshot_baseline(account.equity)
    log.info("daily equity snapshot for %s: %.2f USD", risk.today(), baseline)


async def _snapshot_loop(app: FastAPI, settings: ApiSettings) -> None:
    while True:
        try:
            await snapshot_once(app, settings)
        except Exception as exc:
            log.warning("daily equity snapshot failed: %s", type(exc).__name__)
        await asyncio.sleep(SNAPSHOT_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = api_settings()
    configure_logging(settings.log_level)
    # trust_env=False: calls to execution stay on the internal network and
    # must never be routed through a proxy picked up from the environment.
    app.state.execution_client = httpx.AsyncClient(
        base_url=settings.execution_url, timeout=15.0, trust_env=False
    )
    app.state.risk_controls = (
        RiskControls(
            Redis.from_url(
                settings.redis_url.get_secret_value(),
                decode_responses=True,
                socket_timeout=3,
                socket_connect_timeout=3,
            )
        )
        if settings.redis_url is not None
        else None
    )
    app.state.background_tasks = set()
    snapshots = asyncio.create_task(_snapshot_loop(app, settings))
    yield
    for task in (snapshots, *app.state.background_tasks):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if app.state.risk_controls is not None:
        await app.state.risk_controls.aclose()
    await app.state.execution_client.aclose()


app = FastAPI(title="OpenBroker API", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(orders.router)
app.include_router(admin.router)
