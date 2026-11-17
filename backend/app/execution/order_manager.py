"""Order placement with idempotency on client_order_id.

Results are kept in memory, so idempotency does not survive a restart. A durable
store is part of the execution bundle in the plan.
"""

import asyncio

from app.common.schemas import OrderIntent, OrderResult
from app.execution.broker.base import Broker


class OrderManager:
    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._results: dict[str, OrderResult] = {}
        self._lock = asyncio.Lock()

    @property
    def broker_name(self) -> str:
        return self._broker.name

    async def submit(self, intent: OrderIntent) -> OrderResult:
        # One lock for the whole placement: a retry that arrives while the first
        # attempt is still in flight must wait for it, not send a second order.
        async with self._lock:
            existing = self._results.get(intent.client_order_id)
            if existing is not None:
                return existing
            result = await self._broker.place_order(intent)
            self._results[intent.client_order_id] = result
            return result

    async def aclose(self) -> None:
        await self._broker.aclose()
