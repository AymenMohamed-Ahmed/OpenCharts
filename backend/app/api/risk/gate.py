"""Pre-trade risk gate. Every order passes through here before execution."""

from dataclasses import dataclass

from app.common.config import ApiSettings
from app.common.schemas import OrderIntent


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


def evaluate(intent: OrderIntent, settings: ApiSettings) -> RiskDecision:
    if not settings.trading_enabled:
        return RiskDecision(False, "trading is disabled (TRADING_ENABLED=false)")

    allowed = settings.allowed_symbol_set
    if allowed and intent.symbol.upper() not in allowed:
        return RiskDecision(False, f"symbol {intent.symbol} is not in ALLOWED_SYMBOLS")

    # Reduce-only orders can only shrink exposure, so the size cap does not apply.
    if not intent.reduce_only and intent.notional_usd > settings.max_order_notional_usd:
        return RiskDecision(
            False,
            f"notional {intent.notional_usd:.2f} USD exceeds the "
            f"{settings.max_order_notional_usd:.2f} USD per-order limit",
        )
    return RiskDecision(True)
