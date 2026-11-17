"""Execution service. Reachable only from the gateway, on an internal network."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.auth import require_bearer
from app.common.config import ExecutionSettings, execution_settings
from app.common.logging import configure_logging
from app.common.schemas import Health, OrderIntent, OrderResult
from app.execution.broker.base import Broker
from app.execution.broker.hyperliquid import HyperliquidBroker
from app.execution.broker.paper import PaperBroker
from app.execution.order_manager import OrderManager

_bearer = HTTPBearer(auto_error=False)


def build_broker(settings: ExecutionSettings) -> Broker:
    if settings.broker == "hyperliquid":
        return HyperliquidBroker(settings)
    return PaperBroker()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = execution_settings()
    configure_logging(settings.log_level)
    app.state.orders = OrderManager(build_broker(settings))
    yield
    await app.state.orders.aclose()


app = FastAPI(title="OpenBroker execution", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> Health:
    return Health(service="execution")


@app.post("/internal/orders")
async def place_order(
    intent: OrderIntent,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[ExecutionSettings, Depends(execution_settings)],
) -> OrderResult:
    require_bearer(credentials, settings.execution_internal_token)
    manager: OrderManager = request.app.state.orders
    try:
        return await manager.submit(intent)
    except NotImplementedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
