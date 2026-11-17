"""Calls from the gateway to the execution service."""

import math

import httpx
from fastapi import HTTPException, Request, status

from app.common.config import ApiSettings
from app.common.schemas import AccountState

# Execution statuses the gateway passes through unchanged; anything else is a 502.
_PASS_THROUGH = {404, 409, 503}


def _headers(settings: ApiSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.execution_internal_token.get_secret_value()}"}


async def call_execution(
    request: Request, settings: ApiSettings, method: str, path: str, json: dict | None = None
) -> dict:
    client: httpx.AsyncClient = request.app.state.execution_client
    try:
        response = await client.request(method, path, json=json, headers=_headers(settings))
    except httpx.HTTPError as exc:
        # For an order: it may or may not have reached the broker. The caller
        # retries with the same client_order_id, which execution treats as idempotent.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="execution service unreachable"
        ) from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        code = (
            response.status_code
            if response.status_code in _PASS_THROUGH
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(code, detail=str(detail or response.text)[:500])
    return response.json()


async def fetch_account(request: Request, settings: ApiSettings) -> AccountState:
    account = AccountState.model_validate(
        await call_execution(request, settings, "GET", "/internal/account")
    )
    if not math.isfinite(account.equity):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="execution reported no equity")
    return account
