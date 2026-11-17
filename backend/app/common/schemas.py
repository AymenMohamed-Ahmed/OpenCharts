"""Contracts shared between services."""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    REJECTED = "rejected"


class OrderIntent(BaseModel):
    """A request to trade. Agents and the terminal produce these; only the
    gateway may forward one to execution, after the risk gate passes it."""

    client_order_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    symbol: str = Field(min_length=1, max_length=32)
    side: Side
    quantity: float = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(default=None, gt=0)
    reference_price: float | None = Field(default=None, gt=0)
    reduce_only: bool = False

    @model_validator(mode="after")
    def _price_present(self) -> "OrderIntent":
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders need limit_price")
        if self.order_type is OrderType.MARKET and self.reference_price is None:
            raise ValueError("market orders need reference_price so notional can be checked")
        return self

    @property
    def notional_usd(self) -> float:
        price = self.limit_price if self.order_type is OrderType.LIMIT else self.reference_price
        return self.quantity * (price or 0.0)


class OrderResult(BaseModel):
    client_order_id: str
    status: OrderStatus
    broker: str
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    average_price: float | None = None
    detail: str | None = None


class Health(BaseModel):
    service: str
    status: str = "ok"
