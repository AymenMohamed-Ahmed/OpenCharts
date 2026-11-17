import pytest

from app.common import config


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    for key in (
        "BROKER",
        "HL_NETWORK",
        "HL_ACCOUNT_ADDRESS",
        "HL_AGENT_PRIVATE_KEY",
        "LIVE_TRADING",
        "MAX_SLIPPAGE_BPS",
        "MAX_DAILY_LOSS_USD",
        "REDIS_URL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_AUTH_TOKEN", "test-api-token")
    monkeypatch.setenv("EXECUTION_INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MAX_ORDER_NOTIONAL_USD", "1000")
    monkeypatch.setenv("ALLOWED_SYMBOLS", "")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.db"))
    for cached in (
        config.api_settings,
        config.agent_settings,
        config.ingestion_settings,
        config.execution_settings,
    ):
        cached.cache_clear()
    yield


def order(**overrides):
    body = {
        "client_order_id": "order-0001",
        "symbol": "BTC",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
        "reference_price": 50000,
    }
    body.update(overrides)
    return body
