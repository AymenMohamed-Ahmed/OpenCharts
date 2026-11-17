"""Agent contract.

An agent turns market context into order intents. It never talks to a broker:
its container has no route to the execution service and holds no broker keys.
Intents go to the gateway, where the risk gate decides.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.common.schemas import OrderIntent


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    last_price: float
    features: dict[str, float] = field(default_factory=dict)


class Agent(ABC):
    name: str

    @abstractmethod
    async def propose(self, context: MarketContext) -> list[OrderIntent]: ...
