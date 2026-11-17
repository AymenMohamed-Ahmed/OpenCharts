"""Hyperliquid error text -> a message a trader can act on.

Ported from `humanizeHlError` and `redactSecrets` in src/hyperliquid/orders.ts.
Builder-fee rules are left out: agent orders carry no builder code.
"""

import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), message)
    for pattern, message in (
        (r"insufficient margin", "Not enough margin for this order. Reduce the size or add funds."),
        (
            r"minimum value of \$?10|min(imum)? (order )?value",
            "Order value is below Hyperliquid's $10 minimum. Increase the size.",
        ),
        (
            r"reduce only order would increase|reduce only.*(increase|would)",
            "Reduce-only order rejected: it would increase the position.",
        ),
        (
            r"too far from.*(oracle|reference)|more than \d+% away"
            r"|price.*away from.*(reference|oracle)",
            "Price is too far from the market price.",
        ),
        (
            r"could not immediately match|no liquidity",
            "No liquidity at this price right now, so the order was not filled.",
        ),
        (
            r"post only order would have immediately matched|bad alo",
            "Post-only order would have matched immediately and was rejected.",
        ),
        (
            r"invalid price|tick|price must be divisible",
            "Price isn't valid for this market's tick size.",
        ),
        (
            r"invalid size|size.*(too small|zero)|order has zero size|sz decimals",
            "Order size isn't valid for this market (too small or too many decimals).",
        ),
        (
            r"too many cumulative requests|rate limit|too many requests",
            "Hyperliquid is rate-limiting requests. Wait a few seconds and try again.",
        ),
        (
            r"user or api wallet.*does not exist|api wallet.*not"
            r"|agent.*(not found|expired|invalid)",
            "The API wallet is not authorised for this account (approve it, or it expired).",
        ),
        (
            r"cannot place more than|too many open orders|tooManyOpenOrders",
            "Too many open orders on Hyperliquid. Cancel some and try again.",
        ),
        (
            r"open interest|oi cap|perpMaxPosition|max position",
            "The market has reached its open-interest or position cap. Reduce the size.",
        ),
        (
            r"already canceled|never placed|already filled|cannot modify (canceled|filled)",
            "That order is already filled or cancelled.",
        ),
        (r"insufficient spot balance|insufficient balance", "Insufficient balance."),
        (r"not enough.*(available|free)", "Not enough free balance for this action."),
    )
)

# 32+ bytes of hex: private keys, signatures, hashes.
_SECRET_RE = re.compile(r"(0x)?[0-9a-fA-F]{64,}")


def redact_secrets(text: str) -> str:
    """Remove anything that looks like a key or a signature."""
    return _SECRET_RE.sub("[redacted]", text)


def humanize_hl_error(raw: str) -> str:
    clean = redact_secrets(raw).strip()
    for pattern, message in _RULES:
        if pattern.search(clean):
            return message
    if clean:
        return f"Hyperliquid rejected the order: {clean[:200]}"
    return "Hyperliquid rejected the order."
