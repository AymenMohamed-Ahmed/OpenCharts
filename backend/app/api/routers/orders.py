"""Order entry. The only path from the outside world to the execution service."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.execution_client import call_execution, fetch_account
from app.api.risk.controls import RiskControls, RiskStateUnavailable
from app.api.risk.gate import evaluate
from app.common.auth import require_bearer
from app.common.config import ApiSettings, api_settings
from app.common.schemas import OrderIntent, OrderResult

router = APIRouter(prefix="/v1/orders", tags=["orders"])
_bearer = HTTPBearer(auto_error=False)
ClientOrderId = Annotated[str, Path(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def _authenticated(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[ApiSettings, Depends(api_settings)],
) -> ApiSettings:
    require_bearer(credentials, settings.api_auth_token)
    return settings


def controls(request: Request) -> RiskControls:
    found: RiskControls | None = getattr(request.app.state, "risk_controls", None)
    if found is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="risk state store is not configured (REDIS_URL); refusing orders",
        )
    return found


Settings = Annotated[ApiSettings, Depends(_authenticated)]


async def check_controls(intent: OrderIntent, request: Request, settings: ApiSettings) -> None:
    """Kill switch, then the daily-loss limit. Fails closed."""
    risk = controls(request)
    try:
        state = await risk.trading_state()
        if not state.armed:
            raise HTTPException(422, detail=f"trading is halted: {state.reason}")
        # Reduce-only orders can only shrink exposure, so the loss limit does not stop them.
        if intent.reduce_only:
            return
        account = await fetch_account(request, settings)
        baseline = await risk.snapshot_baseline(account.equity)
    except RiskStateUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{exc}; refusing orders"
        ) from None
    loss = baseline - account.equity
    if loss >= settings.max_daily_loss_usd:
        raise HTTPException(
            422,
            detail=(
                f"daily loss limit reached: equity {account.equity:.2f} USD is {loss:.2f} USD "
                f"below today's start ({baseline:.2f}); limit {settings.max_daily_loss_usd:.2f} "
                "USD. Only reduce-only orders are accepted until 00:00 UTC."
            ),
        )


@router.post("")
async def submit_order(intent: OrderIntent, request: Request, settings: Settings) -> OrderResult:
    decision = evaluate(intent, settings)
    if not decision.allowed:
        raise HTTPException(422, detail=decision.reason)
    await check_controls(intent, request, settings)
    body = await call_execution(
        request, settings, "POST", "/internal/orders", intent.model_dump(mode="json")
    )
    return OrderResult.model_validate(body)


@router.get("/{client_order_id}")
async def order_status(
    client_order_id: ClientOrderId, request: Request, settings: Settings
) -> OrderResult:
    body = await call_execution(request, settings, "GET", f"/internal/orders/{client_order_id}")
    return OrderResult.model_validate(body)


@router.post("/{client_order_id}/cancel")
async def cancel_order(
    client_order_id: ClientOrderId, request: Request, settings: Settings
) -> OrderResult:
    # Cancels reduce risk, so neither the kill switch nor the loss limit blocks them.
    body = await call_execution(
        request, settings, "POST", f"/internal/orders/{client_order_id}/cancel"
    )
    return OrderResult.model_validate(body)
