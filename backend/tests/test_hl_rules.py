"""Cases ported from src/hyperliquid/__tests__/symbols.test.ts and orders.test.ts."""

import math
import random
from decimal import Decimal

import pytest

from app.execution.broker.hl_rules import (
    OrderRuleError,
    allowed_price_decimals,
    format_hl_price,
    format_hl_size,
    format_px,
    format_sz,
    min_size_for,
    slippage_px,
)


@pytest.mark.parametrize(
    ("price", "sz", "expected"),
    [
        # keeps integers whole, whatever the digit count
        (76101, 5, "76101"),
        (1234567, 5, "1234567"),
        # limits non-integers to 5 significant figures
        (76101.5, 5, "76102"),
        (1234.56789, 2, "1234.6"),
        (1.116789, 1, "1.1168"),
        (0.0123456, 0, "0.012346"),
        # limits decimals to 6 - szDecimals
        (0.123456, 4, "0.12"),
        (1.23456, 4, "1.23"),
        (0.00123456, 3, "0.001"),
        # strips trailing zeros and handles round-up carries
        (1.5, 1, "1.5"),
        (9.999996, 0, "10"),
        (0.999996, 0, "1"),
        # invalid input
        (math.nan, 2, "0"),
        (-1, 2, "0"),
        (0, 2, "0"),
    ],
)
def test_format_hl_price(price, sz, expected):
    assert format_hl_price(price, sz) == expected


def test_format_hl_price_always_satisfies_the_rule():
    rng = random.Random(1234)  # noqa: S311 - reproducible test data
    for i in range(2000):
        sz = i % 6
        out = format_hl_price(math.exp(rng.random() * 20 - 8), sz)
        if "." in out:
            integer, dec = out.split(".")
            assert len(dec) <= 6 - sz
            assert len((integer.lstrip("0") + dec).lstrip("0")) <= 5
            assert not out.endswith("0")


@pytest.mark.parametrize(
    ("size", "sz", "expected"),
    [
        # rounds down, never up
        (0.12999, 2, "0.12"),
        (1.999, 0, "1"),
        (0.00019, 4, "0.0001"),
        # absorbs binary floating point noise
        (0.29, 2, "0.29"),
        (0.57, 2, "0.57"),
        (1.1, 1, "1.1"),
        (4.35, 2, "4.35"),
        # strips trailing zeros
        (1.5, 4, "1.5"),
        (2, 3, "2"),
        (100, 0, "100"),
        # non-positive, invalid, dust
        (0, 2, "0"),
        (-1, 2, "0"),
        (math.nan, 2, "0"),
        (0.001, 2, "0"),
    ],
)
def test_format_hl_size(size, sz, expected):
    assert format_hl_size(size, sz) == expected


@pytest.mark.parametrize(
    ("price", "sz", "expected"),
    [(76101, 5, 0), (2400.55, 4, 1), (1.1167, 1, 4), (0.00012345, 0, 6), (1.1167, 4, 2)],
)
def test_allowed_price_decimals(price, sz, expected):
    assert allowed_price_decimals(price, sz) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((65432.123, 5), "65432"),
        ((1234.5678, 3), "1234.6"),
        ((3456.789, 4), "3456.8"),
        ((12.3456789, 2), "12.346"),
        ((0.0012346, 0), "0.001235"),
        ((0.000012345678, 0), "0.000012"),
        ((123456, 5), "123456"),
        ((123456.7, 5), "123457"),
        ((1_000_000, 2), "1000000"),
        ((2.5, 2), "2.5"),
        ((100, 2), "100"),
        ((1.1, 3), "1.1"),
        ((100.123, 2, "up"), "100.13"),
        ((100.123, 2, "down"), "100.12"),
        ((100.127, 2, "nearest"), "100.13"),
        ((100.12, 2, "up"), "100.12"),  # already on the grid: no float drift
    ],
)
def test_format_px(args, expected):
    assert format_px(*args) == expected


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf])
def test_format_px_rejects_bad_prices(bad):
    with pytest.raises(OrderRuleError):
        format_px(bad, 2)


@pytest.mark.parametrize(
    ("size", "sz", "expected"),
    [
        (0.123456, 3, "0.123"),
        (1.9999, 2, "1.99"),
        (0.00099, 3, "0"),
        (5, 0, "5"),
        (5.9, 0, "5"),
        (0.29, 2, "0.29"),
        (0.3, 1, "0.3"),
        (1.1, 1, "1.1"),
    ],
)
def test_format_sz(size, sz, expected):
    assert format_sz(size, sz) == expected


def test_format_sz_rejects_negative():
    with pytest.raises(OrderRuleError):
        format_sz(-1, 2)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((65000, True, 0.03, 5), "66950"),
        ((65000, False, 0.03, 5), "63050"),
        ((100, True, 0.03, 2), "103"),
        ((100, False, 0.03, 2), "97"),
        ((1.2345, True, 0.03, 2), "1.2716"),
        ((1.2345, False, 0.03, 2), "1.1974"),
    ],
)
def test_slippage_px(args, expected):
    assert slippage_px(*args) == expected


@pytest.mark.parametrize("slippage", [0, 0.9, -0.1])
def test_slippage_px_bounds(slippage):
    with pytest.raises(OrderRuleError):
        slippage_px(100, True, slippage, 2)


def test_min_size_for():
    assert min_size_for(65000, 5) == Decimal("0.00016")
    assert min_size_for(2, 0) == Decimal(5)
