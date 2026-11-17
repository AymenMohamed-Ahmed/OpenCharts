"""Order entry. The only path from the outside world to the execution service."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.risk.gate import evaluate
from app.common.auth import require_bearer
from app.common.config import ApiSettings, api_settings
from app.common.schemas import OrderIntent, OrderResult

router = APIRouter(prefix="/v1/orders", tags=["orders"])
_bearer = HTTPBearer(auto_error=False)


def _authenticated(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[ApiSettings, Depends(api_settings)],
) -> ApiSettings:
    require_bearer(credentials, settings.api_auth_token)
    return settings


@router.post("")
async def submit_order(
    intent: OrderIntent,
    request: Request,
    settings: Annotated[ApiSettings, Depends(_authenticated)],
) -> OrderResult:
    decision = evaluate(intent, settings)
    if not decision.allowed:
        raise HTTPException(422, detail=decision.reason)

    client: httpx.AsyncClient = request.app.state.execution_client
    try:
        response = await client.post(
            "/internal/orders",
            json=intent.model_dump(mode="json"),
            headers={
                "Authorization": f"Bearer {settings.execution_internal_token.get_secret_value()}"
            },
        )
    except httpx.HTTPError as exc:
        # The order may or may not have reached the broker. The caller retries
        # with the same client_order_id, which execution treats as idempotent.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="execution service unreachable"
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=response.text[:500])
    return OrderResult.model_validate(response.json())
