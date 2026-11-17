from fastapi.testclient import TestClient

from app.agents.main import app as agents_app
from app.common.config import IngestionSettings
from app.ingestion.main import app as ingestion_app
from app.ingestion.sources.hyperliquid import HyperliquidSource


def test_agents_health_and_registry():
    with TestClient(agents_app) as client:
        assert client.get("/healthz").json()["service"] == "agents"
        assert client.get("/internal/agents").json() == ["flat"]


def test_ingestion_health():
    with TestClient(ingestion_app) as client:
        assert client.get("/healthz").json()["service"] == "ingestion"


def test_hyperliquid_source_urls_follow_the_network():
    testnet = HyperliquidSource(IngestionSettings(hl_network="testnet"))
    mainnet = HyperliquidSource(IngestionSettings(hl_network="mainnet"))
    assert testnet.rest_url == "https://api.hyperliquid-testnet.xyz/info"
    assert testnet.ws_url == "wss://api.hyperliquid-testnet.xyz/ws"
    assert mainnet.ws_url == "wss://api.hyperliquid.xyz/ws"
