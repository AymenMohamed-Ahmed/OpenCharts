"""Simulated venue: fills every order in full at its stated price.

No slippage, queueing or partial fills. It exists so the order path can be
exercised end to end without credentials; it says nothing about real fills.
"""

from app.common.schemas import OrderIntent, OrderResult, OrderStatus, OrderType
from app.execution.broker.base import Broker


class PaperBroker(Broker):
    name = "paper"

    async def place_order(self, intent: OrderIntent) -> OrderResult:
        price = (
            intent.limit_price if intent.order_type is OrderType.LIMIT else intent.reference_price
        )
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            broker=self.name,
            broker_order_id=f"paper-{intent.client_order_id}",
            filled_quantity=intent.quantity,
            average_price=price,
            detail="simulated fill",
        )
