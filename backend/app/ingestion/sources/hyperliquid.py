"""Hyperliquid market data. Public endpoints, no credentials.

The websocket stream is not implemented yet (plan: ingestion bundle).
"""

from collections.abc import AsyncIterator

from app.common.config import IngestionSettings
from app.ingestion.sources.base import MarketDataSource, Tick


class HyperliquidSource(MarketDataSource):
    name = "hyperliquid"

    def __init__(self, settings: IngestionSettings) -> None:
        self.rest_url = f"{settings.hl_api_url}/info"
        self.ws_url = settings.hl_api_url.replace("https://", "wss://") + "/ws"

    def stream(self, symbols: list[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError("Hyperliquid market-data stream is not implemented yet")
