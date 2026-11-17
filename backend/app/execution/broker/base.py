from abc import ABC, abstractmethod

from app.common.schemas import OrderIntent, OrderResult


class Broker(ABC):
    """One venue. Implementations hold the venue credentials."""

    name: str

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> OrderResult: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
