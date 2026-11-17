"""Simulated venue: fills every order in full at its stated price.

No slippage, queueing, partial fills, positions or PnL. It exists so the order
path can be exercised end to end without credentials; it says nothing about
real fills. Its account equity is a constant, so the daily-loss limit never
trips against it.
"""

from app.common.schemas import (
    AccountState,
    CancelAllResult,
    OrderIntent,
    OrderResult,
    OrderStatus,
    OrderType,
)
from app.execution.broker.base import Broker, PreparedOrder
from app.execution.ledger import LedgerRow

PAPER_EQUITY_USD = 10_000.0


class PaperBroker(Broker):
    name = "paper"

    async def prepare(self, intent: OrderIntent, cloid: str) -> PreparedOrder:
        return PreparedOrder(
            cloid=cloid, coin=intent.symbol.upper(), asset=None, request=None, expires_at_ms=None
        )

    async def send(self, intent: OrderIntent, order: PreparedOrder) -> OrderResult:
        price = (
            intent.limit_price if intent.order_type is OrderType.LIMIT else intent.reference_price
        )
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            broker=self.name,
            broker_order_id=f"paper-{intent.client_order_id}",
            cloid=order.cloid,
            filled_quantity=intent.quantity,
            average_price=price,
            detail="simulated fill",
        )

    async def resend(self, row: LedgerRow) -> OrderResult:
        # A paper order left pending by a crash was never filled; send it now.
        return await self.send(row.intent, await self.prepare(row.intent, row.cloid))

    async def lookup(self, row: LedgerRow) -> OrderResult | None:
        return None if row.status is OrderStatus.PENDING else row.result()

    async def cancel(self, row: LedgerRow) -> OrderResult:
        return row.result()

    async def cancel_all(self) -> CancelAllResult:
        return CancelAllResult(broker=self.name, detail="paper orders fill at once")

    async def account(self) -> AccountState:
        return AccountState(
            broker=self.name, equity=PAPER_EQUITY_USD, withdrawable=PAPER_EQUITY_USD
        )
