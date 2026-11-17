"""Hyperliquid venue.

Signing and order placement are not implemented yet (plan: execution bundle).
The class exists so configuration, the egress route and the live-trading guard
are in place and tested before any code can send a real order.

Credentials are an API ("agent") wallet key approved by the account. An agent
key can trade but cannot withdraw, which bounds the damage if it leaks.
"""

import httpx

from app.common.config import ExecutionSettings
from app.common.schemas import OrderIntent, OrderResult
from app.execution.broker.base import Broker


class HyperliquidBroker(Broker):
    name = "hyperliquid"

    def __init__(self, settings: ExecutionSettings) -> None:
        self._settings = settings
        # trust_env=True on purpose: the container has no route out except the
        # allowlisting proxy named by HTTPS_PROXY.
        self._client = httpx.AsyncClient(base_url=settings.hl_api_url, timeout=10.0, trust_env=True)

    async def place_order(self, intent: OrderIntent) -> OrderResult:
        raise NotImplementedError("Hyperliquid order placement is not implemented yet")

    async def aclose(self) -> None:
        await self._client.aclose()
