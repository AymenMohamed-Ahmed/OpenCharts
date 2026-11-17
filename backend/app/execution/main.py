"""Execution service. Reachable only from the gateway, on an internal network."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.auth import require_bearer
from app.common.config import ExecutionSettings, execution_settings
from app.common.logging import configure_logging
from app.common.schemas import AccountState, CancelAllResult, Health, OrderIntent, OrderResult
from app.execution.broker.base import Broker, BrokerUnavailable
from app.execution.broker.hyperliquid import HyperliquidBroker
from app.execution.broker.paper import PaperBroker
from app.execution.ledger import Ledger
from app.execution.order_manager import IdempotencyConflict, OrderManager, OrderNotFound

_bearer = HTTPBearer(auto_error=False)
ClientOrderId = Annotated[str, Path(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def build_broker(settings: ExecutionSettings) -> Broker:
    if settings.broker == "hyperliquid":
        return HyperliquidBroker(settings)
    return PaperBroker()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = execution_settings()
    configure_logging(settings.log_level)
    manager = OrderManager(build_broker(settings), Ledger(settings.ledger_path))
    await manager.start()
    app.state.orders = manager
    yield
    await manager.aclose()


app = FastAPI(title="OpenBroker execution", version="0.1.0", lifespan=lifespan)


def _authorised(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[ExecutionSettings, Depends(execution_settings)],
) -> None:
    require_bearer(credentials, settings.execution_internal_token)


def _manager(request: Request) -> OrderManager:
    return request.app.state.orders


Manager = Annotated[OrderManager, Depends(_manager)]
Auth = Depends(_authorised)


@app.get("/healthz")
def healthz() -> Health:
    return Health(service="execution")


@app.post("/internal/orders", dependencies=[Auth])
async def place_order(intent: OrderIntent, manager: Manager) -> OrderResult:
    try:
        return await manager.submit(intent)
    except IdempotencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None


@app.get("/internal/orders/{client_order_id}", dependencies=[Auth])
async def order_status(client_order_id: ClientOrderId, manager: Manager) -> OrderResult:
    try:
        return await manager.status(client_order_id)
    except OrderNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown client_order_id") from None


@app.post("/internal/orders/{client_order_id}/cancel", dependencies=[Auth])
async def cancel_order(client_order_id: ClientOrderId, manager: Manager) -> OrderResult:
    try:
        return await manager.cancel(client_order_id)
    except OrderNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown client_order_id") from None
    except BrokerUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None


@app.post("/internal/cancel-all", dependencies=[Auth])
async def cancel_all(manager: Manager) -> CancelAllResult:
    try:
        return await manager.cancel_all()
    except BrokerUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None


@app.get("/internal/account", dependencies=[Auth])
async def account(manager: Manager) -> AccountState:
    try:
        return await manager.account()
    except BrokerUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
