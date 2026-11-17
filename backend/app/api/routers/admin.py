"""Operator controls: kill switch, resume, status. Separate ADMIN_TOKEN."""

import asyncio
import contextlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.api.execution_client import call_execution, fetch_account
from app.api.risk.controls import RiskStateUnavailable, TradingState
from app.api.routers.orders import controls
from app.common.auth import require_bearer
from app.common.config import ApiSettings, api_settings
from app.common.schemas import CancelAllResult

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin", tags=["admin"])
_bearer = HTTPBearer(auto_error=False)

# A second cancel-all after a kill catches an order that passed the kill check
# just before the switch flipped and reached the venue after the first pass.
SECOND_CANCEL_DELAY_S = 5.0


def _admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[ApiSettings, Depends(api_settings)],
) -> ApiSettings:
    if settings.admin_token is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="ADMIN_TOKEN is not set")
    require_bearer(credentials, settings.admin_token)
    return settings


Admin = Annotated[ApiSettings, Depends(_admin)]


class KillRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


def _state(state: TradingState) -> dict:
    return {"trading": state.label, "reason": state.reason, "since": state.since}


async def _cancel_all(request: Request, settings: ApiSettings) -> CancelAllResult:
    body = await call_execution(request, settings, "POST", "/internal/cancel-all")
    return CancelAllResult.model_validate(body)


async def _second_pass(request: Request, settings: ApiSettings) -> None:
    await asyncio.sleep(SECOND_CANCEL_DELAY_S)
    try:
        result = await _cancel_all(request, settings)
        log.info("kill: second cancel-all pass cancelled %d orders", result.canceled)
    except Exception as exc:
        log.error("kill: second cancel-all pass failed: %s", type(exc).__name__)


@router.post("/kill")
async def kill(request: Request, settings: Admin, body: KillRequest | None = None) -> JSONResponse:
    """Halt trading, then cancel every open order. The halt is recorded first,
    so no new order passes the gate while orders are being cancelled."""
    risk = controls(request)
    halted = None
    store_error = None
    try:
        halted = await risk.kill(body.reason if body else None)
    except RiskStateUnavailable as exc:
        # The gate already refuses orders while the store is unreachable.
        store_error = str(exc)
    log.warning("kill switch engaged%s", f": {store_error}" if store_error else "")

    cancel: dict = {}
    code = status.HTTP_200_OK
    try:
        cancel = (await _cancel_all(request, settings)).model_dump()
    except HTTPException as exc:
        cancel = {"error": str(exc.detail)}
        code = status.HTTP_502_BAD_GATEWAY
    tasks = request.app.state.background_tasks
    task = asyncio.create_task(_second_pass(request, settings))
    tasks.add(task)
    task.add_done_callback(tasks.discard)

    content = _state(halted) if halted else {"trading": "unknown", "reason": store_error}
    if store_error or cancel.get("failed"):
        code = status.HTTP_502_BAD_GATEWAY
    return JSONResponse(status_code=code, content={**content, "cancel_all": cancel})


@router.post("/resume")
async def resume(request: Request, settings: Admin) -> dict:
    try:
        state = await controls(request).resume()
    except RiskStateUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    log.warning("trading resumed by operator")
    return {**_state(state), "trading_enabled": settings.trading_enabled}


@router.get("/status")
async def admin_status(request: Request, settings: Admin) -> dict:
    risk = controls(request)
    try:
        state = await risk.trading_state()
        baseline = await risk.day_baseline()
    except RiskStateUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    equity = None
    with contextlib.suppress(HTTPException):
        equity = (await fetch_account(request, settings)).equity
    return {
        **_state(state),
        "trading_enabled": settings.trading_enabled,
        "day": risk.today(),
        "start_equity": baseline,
        "equity": equity,
        "loss": baseline - equity if baseline is not None and equity is not None else None,
        "max_daily_loss_usd": settings.max_daily_loss_usd,
    }
