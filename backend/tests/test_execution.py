import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.common.config import ExecutionSettings
from app.execution.main import app
from tests.conftest import order

AUTH = {"Authorization": "Bearer test-internal-token"}


def test_health():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"service": "execution", "status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_orders_need_the_internal_token(headers):
    with TestClient(app) as client:
        assert client.post("/internal/orders", json=order(), headers=headers).status_code == 401


def test_paper_fill():
    with TestClient(app) as client:
        body = client.post("/internal/orders", json=order(), headers=AUTH).json()
    assert body["status"] == "filled"
    assert body["broker"] == "paper"
    assert body["filled_quantity"] == 0.01
    assert body["average_price"] == 50000


def test_same_client_order_id_is_not_placed_twice():
    with TestClient(app) as client:
        first = client.post("/internal/orders", json=order(), headers=AUTH).json()
        # Same id, different size: the stored result wins, no second order.
        second = client.post("/internal/orders", json=order(quantity=5), headers=AUTH).json()
    assert second == first


def test_hyperliquid_needs_credentials():
    with pytest.raises(ValidationError, match="HL_ACCOUNT_ADDRESS"):
        ExecutionSettings(execution_internal_token="t", broker="hyperliquid")


def test_hyperliquid_mainnet_needs_live_trading_flag():
    creds = {
        "execution_internal_token": "t",
        "broker": "hyperliquid",
        "hl_account_address": "0x" + "1" * 40,
        "hl_agent_private_key": "0x" + "2" * 64,
    }
    with pytest.raises(ValidationError, match="LIVE_TRADING"):
        ExecutionSettings(**creds, hl_network="mainnet")
    assert ExecutionSettings(**creds, hl_network="mainnet", live_trading=True).live_trading
    assert ExecutionSettings(**creds, hl_network="testnet").hl_api_url.endswith("testnet.xyz")


def test_hyperliquid_orders_report_not_implemented(monkeypatch):
    monkeypatch.setenv("BROKER", "hyperliquid")
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("HL_AGENT_PRIVATE_KEY", "0x" + "2" * 64)
    with TestClient(app) as client:
        response = client.post("/internal/orders", json=order(), headers=AUTH)
    assert response.status_code == 501
