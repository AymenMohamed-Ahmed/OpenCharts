from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    timestamp_ms: int


class MarketDataSource(ABC):
    name: str

    @abstractmethod
    def stream(self, symbols: list[str]) -> AsyncIterator[Tick]: ...
