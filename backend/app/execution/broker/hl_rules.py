"""Hyperliquid price and size rules for core perps.

Ported from the terminal: `src/hyperliquid/symbols.ts` (`allowedPriceDecimals`,
`formatHlPrice`, `formatHlSize`) and `src/hyperliquid/orders.ts` (`formatPx`,
`formatSz`, `slippagePx`, `minSizeFor`). The tests mirror the terminal's cases.

Arithmetic is done in `Decimal`, built from the shortest repr of each float, so
values such as 0.29 or 100.12 are exact and binary noise cannot move a price
or size across a tick.

Rules (perps): a price has at most 5 significant figures and at most
`6 - szDecimals` decimals; an integer price is always valid. A size has at most
`szDecimals` decimals and is always rounded down, never up.
"""

import math
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal

MAX_PRICE_SIG_FIGS = 5
MAX_PERP_DECIMALS = 6
# Hyperliquid refuses non-reduce-only orders worth less than this (USD).
MIN_ORDER_NOTIONAL_USD = Decimal(10)

Rounding = Literal["nearest", "up", "down"]
_ROUNDING = {"nearest": ROUND_HALF_UP, "up": ROUND_CEILING, "down": ROUND_FLOOR}


class OrderRuleError(ValueError):
    """An order that cannot be expressed on Hyperliquid. The message is safe to return."""

    def __init__(self, message: str, code: str = "validation") -> None:
        super().__init__(message)
        self.code = code


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def _valid(x: float) -> bool:
    return isinstance(x, int | float) and math.isfinite(x) and x > 0


def _strip(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _max_decimals(sz_decimals: int) -> int:
    return max(0, MAX_PERP_DECIMALS - sz_decimals)


def allowed_price_decimals(price: float, sz_decimals: int) -> int:
    """Decimals allowed for a perp price of magnitude `price`."""
    max_dec = _max_decimals(sz_decimals)
    if not _valid(price):
        return max_dec
    magnitude = _dec(price).adjusted()  # 76101 -> 4, 1.11 -> 0, 0.012 -> -2
    return min(max_dec, max(0, MAX_PRICE_SIG_FIGS - 1 - magnitude))


def format_px(price: float, sz_decimals: int, rounding: Rounding = "nearest") -> str:
    """A price string Hyperliquid accepts. Raises on non-positive input or a
    price that rounds to zero."""
    if not _valid(price):
        raise OrderRuleError("Price must be a positive number.")
    decimals = allowed_price_decimals(price, sz_decimals)
    rounded = _dec(price).quantize(Decimal(1).scaleb(-decimals), rounding=_ROUNDING[rounding])
    if rounded <= 0:
        raise OrderRuleError("Price is below the smallest tick for this market.")
    return _strip(rounded)


def format_sz(size: float, sz_decimals: int) -> str:
    """A size string, rounded DOWN to `sz_decimals`. Dust becomes "0"."""
    if not (isinstance(size, int | float) and math.isfinite(size)) or size < 0:
        raise OrderRuleError("Size must be a positive number.")
    d = max(0, int(sz_decimals))
    return _strip(_dec(size).quantize(Decimal(1).scaleb(-d), rounding=ROUND_DOWN))


def format_hl_price(price: float, sz_decimals: int) -> str:
    """Like `format_px` (nearest), but returns "0" for invalid input."""
    try:
        return format_px(price, sz_decimals)
    except OrderRuleError:
        return "0"


def format_hl_size(size: float, sz_decimals: int) -> str:
    """Like `format_sz`, but returns "0" for invalid input."""
    if not _valid(size):
        return "0"
    return format_sz(size, sz_decimals)


def slippage_px(ref: float, is_buy: bool, slippage: float, sz_decimals: int) -> str:
    """Marketable limit price: `ref` pushed `slippage` through the book, rounded
    in the aggressive direction (up for a buy, down for a sell)."""
    if not (isinstance(slippage, int | float) and 0 < slippage <= 0.5):
        raise OrderRuleError("Slippage must be between 0 and 50%.")
    if not _valid(ref):
        raise OrderRuleError("Price must be a positive number.")
    factor = 1 + _dec(slippage) if is_buy else 1 - _dec(slippage)
    px = _dec(ref) * factor
    decimals = allowed_price_decimals(float(px), sz_decimals)
    mode = _ROUNDING["up" if is_buy else "down"]
    rounded = px.quantize(Decimal(1).scaleb(-decimals), rounding=mode)
    if rounded <= 0:
        raise OrderRuleError("Price is below the smallest tick for this market.")
    return _strip(rounded)


def min_size_for(ref: float, sz_decimals: int) -> Decimal:
    """Smallest size at `sz_decimals` whose notional at `ref` reaches $10."""
    if not _valid(ref):
        raise OrderRuleError("Price must be a positive number.")
    step = Decimal(1).scaleb(-max(0, int(sz_decimals)))
    return (MIN_ORDER_NOTIONAL_USD / _dec(ref)).quantize(step, rounding=ROUND_CEILING)
