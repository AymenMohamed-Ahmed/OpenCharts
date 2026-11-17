import asyncio
import json

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import app as api_app
from app.api.main import snapshot_once
from app.api.risk.controls import RiskControls
from app.common.config import ApiSettings, api_settings
from app.execution.main import app as execution_app
from tests.conftest import order

AUTH = {"Authorization": "Bearer test-api-token"}
ADMIN = {"Authorization": "Bearer test-admin-token"}
DAY = 1_800_000_000.0  # 2027-01-15 08:00 UTC


class Clock:
    def __init__(self) -> None:
        self.t = DAY

    def __call__(self) -> float:
        return self.t


def install_redis(api: TestClient, clock=None) -> fakeredis.FakeServer:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    api.app.state.risk_controls = RiskControls(redis, **({"clock": clock} if clock else {}))
    return server


def arm(api: TestClient) -> None:
    assert api.post("/v1/admin/resume", headers=ADMIN).status_code == 200


class Recorder(httpx.AsyncBaseTransport):
    """Real in-process execution app, with a log of the paths the gateway called."""

    def __init__(self) -> None:
        self.inner = httpx.ASGITransport(app=execution_app)
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        return await self.inner.handle_async_request(request)


@pytest.fixture
def client():
    """Gateway wired to a real in-process execution app instead of the network,
    and to an in-memory Redis, armed."""
    with TestClient(execution_app), TestClient(api_app) as api:
        api.recorder = Recorder()
        api.app.state.execution_client = httpx.AsyncClient(
            transport=api.recorder, base_url="http://execution"
        )
        install_redis(api)
        arm(api)
        yield api


def test_health(client):
    assert client.get("/healthz").json() == {"service": "api", "status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer test-internal-token"}, ADMIN],
)
def test_orders_need_the_api_token(client, headers):
    assert client.post("/v1/orders", json=order(), headers=headers).status_code == 401
    assert client.get("/v1/orders/order-0001", headers=headers).status_code == 401
    assert client.post("/v1/orders/order-0001/cancel", headers=headers).status_code == 401


def test_order_passes_risk_and_reaches_execution(client):
    response = client.post("/v1/orders", json=order(), headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "filled"


def test_status_and_cancel_are_proxied(client):
    client.post("/v1/orders", json=order(), headers=AUTH)
    assert client.get("/v1/orders/order-0001", headers=AUTH).json()["status"] == "filled"
    cancel = client.post("/v1/orders/order-0001/cancel", headers=AUTH)
    assert cancel.status_code == 200
    assert client.get("/v1/orders/missing-0001", headers=AUTH).status_code == 404
    assert client.post("/v1/orders/missing-0001/cancel", headers=AUTH).status_code == 404
    assert "/internal/orders/order-0001/cancel" in client.recorder.paths


def test_reused_client_order_id_is_a_conflict(client):
    client.post("/v1/orders", json=order(), headers=AUTH)
    assert client.post("/v1/orders", json=order(quantity=0.02), headers=AUTH).status_code == 409


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


# ── kill switch ─────────────────────────────────────────────────────────────


def test_trading_is_halted_until_armed():
    with TestClient(execution_app), TestClient(api_app) as api:
        api.app.state.execution_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=execution_app), base_url="http://execution"
        )
        install_redis(api)
        refused = api.post("/v1/orders", json=order(), headers=AUTH)
        assert refused.status_code == 422
        assert "not been armed" in refused.json()["detail"]
        arm(api)
        assert api.post("/v1/orders", json=order(), headers=AUTH).status_code == 200


def test_no_redis_configured_refuses_orders():
    with TestClient(execution_app), TestClient(api_app) as api:
        response = api.post("/v1/orders", json=order(), headers=AUTH)
    assert response.status_code == 503


def test_unreachable_redis_refuses_orders(client):
    install_redis(client).connected = False
    response = client.post("/v1/orders", json=order(), headers=AUTH)
    assert response.status_code == 503
    assert "refusing orders" in response.json()["detail"]


def test_kill_halts_trading_and_cancels_everything(client):
    response = client.post("/v1/admin/kill", json={"reason": "drill"}, headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["trading"] == "killed"
    assert body["cancel_all"]["broker"] == "paper"
    assert "/internal/cancel-all" in client.recorder.paths

    refused = client.post("/v1/orders", json=order(), headers=AUTH)
    assert refused.status_code == 422
    assert "drill" in refused.json()["detail"]
    # Even reduce-only orders wait for a resume.
    reduce = client.post("/v1/orders", json=order(reduce_only=True), headers=AUTH)
    assert reduce.status_code == 422

    status = client.get("/v1/admin/status", headers=ADMIN).json()
    assert status["trading"] == "killed"
    arm(client)
    assert client.post("/v1/orders", json=order(), headers=AUTH).status_code == 200


def test_kill_survives_a_gateway_restart(client):
    redis_state = client.app.state.risk_controls
    client.post("/v1/admin/kill", headers=ADMIN)
    with TestClient(api_app) as restarted:
        restarted.app.state.risk_controls = redis_state
        restarted.app.state.execution_client = client.app.state.execution_client
        assert restarted.post("/v1/orders", json=order(), headers=AUTH).status_code == 422


def test_kill_wins_over_trading_enabled_and_trading_enabled_wins_over_resume(client, monkeypatch):
    client.post("/v1/admin/kill", headers=ADMIN)
    assert api_settings().trading_enabled
    assert client.post("/v1/orders", json=order(), headers=AUTH).status_code == 422
    arm(client)
    monkeypatch.setenv("TRADING_ENABLED", "false")
    api_settings.cache_clear()
    refused = client.post("/v1/orders", json=order(), headers=AUTH)
    assert refused.status_code == 422
    assert "TRADING_ENABLED" in refused.json()["detail"]


def test_kill_still_halts_when_cancel_all_fails(client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "venue down"})

    client.app.state.execution_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://execution"
    )
    response = client.post("/v1/admin/kill", headers=ADMIN)
    assert response.status_code == 502
    assert response.json()["trading"] == "killed"
    assert "venue down" in response.json()["cancel_all"]["error"]
    assert client.post("/v1/orders", json=order(), headers=AUTH).status_code == 422


@pytest.mark.parametrize("path", ["/v1/admin/kill", "/v1/admin/resume", "/v1/admin/status"])
@pytest.mark.parametrize("headers", [{}, AUTH, {"Authorization": "Bearer test-internal-token"}])
def test_admin_needs_the_admin_token(client, path, headers):
    method = client.get if path.endswith("status") else client.post
    assert method(path, headers=headers).status_code == 401


def test_admin_disabled_without_a_token(client, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN")
    api_settings.cache_clear()
    assert client.post("/v1/admin/kill", headers=ADMIN).status_code == 503


@pytest.mark.parametrize("token", ["test-api-token", "test-internal-token"])
def test_admin_token_must_be_distinct(token):
    with pytest.raises(ValidationError, match="ADMIN_TOKEN"):
        ApiSettings(
            api_auth_token="test-api-token",
            execution_internal_token="test-internal-token",
            admin_token=token,
        )


# ── daily-loss limit ────────────────────────────────────────────────────────


class FakeExecution:
    def __init__(self) -> None:
        self.equity: float | None = 1000.0
        self.orders: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/account":
            if self.equity is None:
                return httpx.Response(503, json={"detail": "venue down"})
            return httpx.Response(200, json={"broker": "fake", "equity": self.equity})
        if request.url.path == "/internal/orders":
            body = json.loads(request.content)
            self.orders.append(body)
            return httpx.Response(
                200,
                json={
                    "client_order_id": body["client_order_id"],
                    "status": "filled",
                    "broker": "fake",
                },
            )
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def loss_rig(monkeypatch):
    monkeypatch.setenv("MAX_DAILY_LOSS_USD", "50")
    with TestClient(api_app) as api:
        fake = FakeExecution()
        clock = Clock()
        api.app.state.execution_client = httpx.AsyncClient(
            transport=httpx.MockTransport(fake), base_url="http://execution"
        )
        install_redis(api, clock)
        arm(api)
        yield api, fake, clock


def _order(i: int, **overrides):
    return order(client_order_id=f"loss-test-{i:03}", **overrides)


def test_daily_loss_limit(loss_rig):
    api, fake, clock = loss_rig
    assert api.post("/v1/orders", json=_order(1), headers=AUTH).status_code == 200  # baseline 1000
    fake.equity = 960.0
    assert api.post("/v1/orders", json=_order(2), headers=AUTH).status_code == 200
    fake.equity = 950.0  # 50 down: limit reached
    refused = api.post("/v1/orders", json=_order(3), headers=AUTH)
    assert refused.status_code == 422
    assert "daily loss limit" in refused.json()["detail"]
    # Reduce-only orders still pass.
    assert api.post("/v1/orders", json=_order(4, reduce_only=True), headers=AUTH).status_code == 200
    status = api.get("/v1/admin/status", headers=ADMIN).json()
    assert (status["start_equity"], status["loss"]) == (1000.0, 50.0)
    # A new UTC day starts from the current equity.
    clock.t += 24 * 3600
    assert api.post("/v1/orders", json=_order(5), headers=AUTH).status_code == 200
    assert [o["client_order_id"] for o in fake.orders] == [
        "loss-test-001",
        "loss-test-002",
        "loss-test-004",
        "loss-test-005",
    ]


def test_unknown_equity_refuses_new_risk_but_not_reduce_only(loss_rig):
    api, fake, _ = loss_rig
    fake.equity = None
    assert api.post("/v1/orders", json=_order(1), headers=AUTH).status_code == 503
    assert api.post("/v1/orders", json=_order(2, reduce_only=True), headers=AUTH).status_code == 200


def test_midnight_snapshot_sets_the_baseline_before_any_order(loss_rig):
    api, fake, _ = loss_rig
    asyncio.run(snapshot_once(api.app, api_settings()))
    fake.equity = 900.0
    assert api.post("/v1/orders", json=_order(1), headers=AUTH).status_code == 422
