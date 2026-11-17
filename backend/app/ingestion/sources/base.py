from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    coin: str
    asset_index: int
    sz_decimals: int
    max_leverage: int


@dataclass(frozen=True)
class Candle:
    coin: str
    interval: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    timestamp_ms: int


@dataclass(frozen=True)
class Reconnected:
    """Yielded after the stream (re)connects and subscribes. Data may have been
    missed while it was down, so consumers backfill from their last stored bar."""


StreamEvent = Tick | Candle | Reconnected


class MarketDataSource(ABC):
    name: str

    @abstractmethod
    def stream(self, coins: list[str], intervals: list[str]) -> AsyncIterator[StreamEvent]: ...
