import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import app as api_app
from app.execution.main import app as execution_app
from tests.conftest import order

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def client():
    """Gateway wired to a real in-process execution app instead of the network."""
    with TestClient(execution_app), TestClient(api_app) as api:
        api.app.state.execution_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=execution_app), base_url="http://execution"
        )
        yield api


def test_health(client):
    assert client.get("/healthz").json() == {"service": "api", "status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer test-internal-token"}])
def test_orders_need_the_api_token(client, headers):
    assert client.post("/v1/orders", json=order(), headers=headers).status_code == 401


def test_order_passes_risk_and_reaches_execution(client):
    response = client.post("/v1/orders", json=order(), headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "filled"


def test_risk_rejection_never_reaches_execution(client):
    sent = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500)

    client.app.state.execution_client = httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url="http://execution"
    )
    response = client.post("/v1/orders", json=order(quantity=10), headers=AUTH)
    assert response.status_code == 422
    assert "per-order limit" in response.json()["detail"]
    assert sent == []


def test_unreachable_execution_is_a_502(client):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client.app.state.execution_client = httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://execution"
    )
    assert client.post("/v1/orders", json=order(), headers=AUTH).status_code == 502
