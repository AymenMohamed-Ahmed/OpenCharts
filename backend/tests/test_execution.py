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
        again = client.post("/internal/orders", json=order(), headers=AUTH).json()
        # Same id, different order: refused, never placed.
        conflict = client.post("/internal/orders", json=order(quantity=5), headers=AUTH)
    assert again == first
    assert conflict.status_code == 409


def test_idempotency_survives_a_restart():
    with TestClient(app) as client:
        first = client.post("/internal/orders", json=order(), headers=AUTH).json()
    with TestClient(app) as client:
        assert client.get("/internal/orders/order-0001", headers=AUTH).json() == first
        conflict = client.post("/internal/orders", json=order(quantity=5), headers=AUTH)
    assert conflict.status_code == 409


def test_order_status_cancel_account_and_cancel_all():
    with TestClient(app) as client:
        client.post("/internal/orders", json=order(), headers=AUTH)
        assert client.get("/internal/orders/order-0001", headers=AUTH).json()["status"] == "filled"
        cancel = client.post("/internal/orders/order-0001/cancel", headers=AUTH)
        assert cancel.json()["status"] == "filled"  # nothing left to cancel
        assert client.get("/internal/orders/unknown-0001", headers=AUTH).status_code == 404
        assert client.post("/internal/orders/unknown-0001/cancel", headers=AUTH).status_code == 404
        assert client.get("/internal/account", headers=AUTH).json()["equity"] > 0
        assert client.post("/internal/cancel-all", headers=AUTH).json()["canceled"] == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/internal/orders/order-0001"),
        ("post", "/internal/orders/order-0001/cancel"),
        ("post", "/internal/cancel-all"),
        ("get", "/internal/account"),
    ],
)
def test_every_internal_endpoint_needs_the_token(method, path):
    with TestClient(app) as client:
        assert getattr(client, method)(path).status_code == 401
        bad = {"Authorization": "Bearer test-api-token"}
        assert getattr(client, method)(path, headers=bad).status_code == 401


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


def test_hyperliquid_service_wiring(monkeypatch):
    """The service builds the Hyperliquid broker from settings; the fake exchange
    stands in for the network."""
    from app.execution import main
    from app.execution.broker.hyperliquid import HyperliquidBroker
    from tests.fake_hyperliquid import FakeHyperliquid
    from tests.test_hyperliquid_broker import AGENT, AGENT_KEY, USER

    monkeypatch.setenv("BROKER", "hyperliquid")
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", USER)
    monkeypatch.setenv("HL_AGENT_PRIVATE_KEY", AGENT_KEY)
    fake = FakeHyperliquid(agent_address=AGENT, user=USER, clock=__import__("time").time)
    monkeypatch.setattr(
        main, "build_broker", lambda settings: HyperliquidBroker(settings, transport=fake)
    )
    with TestClient(app) as client:
        body = client.post("/internal/orders", json=order(), headers=AUTH).json()
        account = client.get("/internal/account", headers=AUTH).json()
    assert body["status"] == "filled"
    assert body["broker"] == "hyperliquid"
    assert account["equity"] == 1000.0
