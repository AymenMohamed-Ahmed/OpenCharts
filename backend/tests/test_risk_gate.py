import pytest
from pydantic import ValidationError

from app.api.risk.gate import evaluate
from app.common.config import ApiSettings
from app.common.schemas import OrderIntent
from tests.conftest import order


def settings(**overrides) -> ApiSettings:
    base = {
        "api_auth_token": "a",
        "execution_internal_token": "b",
        "trading_enabled": True,
        "max_order_notional_usd": 1000,
    }
    return ApiSettings(**{**base, **overrides})


def test_allows_order_within_limits():
    assert evaluate(OrderIntent(**order()), settings()).allowed


def test_blocks_everything_when_trading_disabled():
    decision = evaluate(OrderIntent(**order()), settings(trading_enabled=False))
    assert not decision.allowed
    assert "disabled" in decision.reason


def test_blocks_notional_above_limit():
    decision = evaluate(OrderIntent(**order(quantity=1)), settings())
    assert not decision.allowed
    assert "50000.00" in decision.reason


def test_notional_at_limit_is_allowed():
    assert evaluate(OrderIntent(**order(quantity=0.02)), settings()).allowed


def test_limit_order_uses_limit_price_for_notional():
    intent = OrderIntent(**order(order_type="limit", limit_price=200000, reference_price=1))
    assert not evaluate(intent, settings()).allowed


def test_reduce_only_is_exempt_from_size_cap():
    assert evaluate(OrderIntent(**order(quantity=1, reduce_only=True)), settings()).allowed


def test_symbol_allowlist():
    restricted = settings(allowed_symbols="eth, sol")
    assert not evaluate(OrderIntent(**order()), restricted).allowed
    assert evaluate(OrderIntent(**order(symbol="ETH")), restricted).allowed


@pytest.mark.parametrize(
    "overrides",
    [
        {"reference_price": None},
        {"order_type": "limit", "limit_price": None},
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": float("inf")},
        {"quantity": float("nan")},
        {"reference_price": float("inf")},
        {"order_type": "limit", "limit_price": float("inf")},
        {"client_order_id": "short"},
        {"client_order_id": "has spaces in it"},
    ],
)
def test_rejects_malformed_intents(overrides):
    with pytest.raises(ValidationError):
        OrderIntent(**order(**overrides))
