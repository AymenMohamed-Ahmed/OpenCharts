"""Hyperliquid venue (core perps only).

Credentials are an API ("agent") wallet key approved by the account. An agent
key can trade but cannot withdraw, which bounds the damage if it leaks. Orders
are signed with the official SDK's `sign_l1_action`; nothing here implements
crypto. Every request leaves through the allowlisting proxy named by
HTTPS_PROXY.

Orders use the account's current leverage and margin mode for the market; this
service never changes them.

Pricing:
  - market: IOC limit at the venue mid pushed MAX_SLIPPAGE_BPS through the
    book. The caller's reference_price (which the gateway used for its notional
    cap) must be within MAX_SLIPPAGE_BPS of the mid, or the order is refused.
  - limit: GTC, price rounded to the tick in the caller's favour (a buy never
    pays more, a sell never receives less, than asked). A limit price that is
    marketable by more than MAX_SLIPPAGE_BPS is refused, so the gateway's
    notional figure stays close to what can actually trade.
"""

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from eth_account import Account
from hyperliquid.utils.signing import order_wires_to_order_action, sign_l1_action

from app.common.config import ExecutionSettings
from app.common.schemas import (
    AccountState,
    CancelAllResult,
    OrderIntent,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
)
from app.execution.broker.base import (
    Broker,
    BrokerUnavailable,
    OutcomeUnknown,
    PreparedOrder,
    RequestRefused,
)
from app.execution.broker.hl_errors import humanize_hl_error, redact_secrets
from app.execution.broker.hl_rules import (
    MIN_ORDER_NOTIONAL_USD,
    OrderRuleError,
    format_px,
    format_sz,
    min_size_for,
    slippage_px,
)
from app.execution.ledger import LedgerRow

log = logging.getLogger(__name__)

ASSET_REFRESH_S = 600.0
ASSET_RETRY_S = 15.0
# A signed order is valid this long. Past it (plus a grace period in the order
# manager) a cloid the venue does not know was never placed.
ORDER_TTL_MS = 30_000
CANCEL_BATCH = 50


@dataclass(frozen=True)
class Asset:
    coin: str
    index: int
    sz_decimals: int
    max_leverage: int
    delisted: bool


def load_agent_wallet(settings: ExecutionSettings):
    """The agent wallet from HL_AGENT_PRIVATE_KEY. Errors never include the key."""
    if settings.hl_agent_private_key is None:
        raise ValueError("HL_AGENT_PRIVATE_KEY is not set")
    try:
        wallet = Account.from_key(settings.hl_agent_private_key.get_secret_value().strip())
    except Exception:
        raise ValueError("HL_AGENT_PRIVATE_KEY is not a valid private key") from None
    if wallet.address.lower() == settings.hl_account_address.lower():
        raise ValueError(
            "HL_AGENT_PRIVATE_KEY is the key of HL_ACCOUNT_ADDRESS itself. Use an API (agent) "
            "wallet approved by the account: it can trade but cannot withdraw."
        )
    return wallet


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _statuses(body: dict[str, Any]) -> list[Any]:
    response = body.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    return statuses if isinstance(statuses, list) else []


def status_from_order_status(raw: str) -> OrderStatus:
    """`orderStatus` status string -> our status."""
    if raw == "open":
        return OrderStatus.ACCEPTED
    if raw == "filled":
        return OrderStatus.FILLED
    if raw == "rejected" or raw.endswith("Rejected"):
        return OrderStatus.REJECTED
    if raw in ("canceled", "scheduledCancel") or raw.endswith("Canceled"):
        return OrderStatus.CANCELED
    # "triggered" and anything new: the venue holds the order, so it is live.
    return OrderStatus.ACCEPTED


class HyperliquidBroker(Broker):
    name = "hyperliquid"

    def __init__(
        self,
        settings: ExecutionSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.time,
    ) -> None:
        self._settings = settings
        self._wallet = load_agent_wallet(settings)
        self._user = settings.hl_account_address.lower()
        self._is_mainnet = settings.hl_network == "mainnet"
        self._slippage = settings.max_slippage_bps / 10_000
        self._clock = clock
        self._last_nonce = 0
        self._assets: dict[str, Asset] = {}
        self._refresher: asyncio.Task[None] | None = None
        # trust_env=True on purpose: the container has no route out except the
        # allowlisting proxy named by HTTPS_PROXY. An injected transport (tests)
        # must never pick up a proxy from the environment.
        self._client = httpx.AsyncClient(
            base_url=settings.hl_api_url,
            timeout=10.0,
            transport=transport,
            trust_env=transport is None,
        )

    @property
    def agent_address(self) -> str:
        return self._wallet.address

    # ── lifecycle and reference data ────────────────────────────────────────

    async def start(self) -> None:
        log.info(
            "hyperliquid %s: account %s, agent %s",
            self._settings.hl_network,
            self._user,
            self.agent_address,
        )
        try:
            await self.refresh_assets()
        except BrokerUnavailable as exc:
            # Start anyway (health checks, cancels by cloid need no map); orders
            # are refused until the map loads.
            log.warning("asset map not loaded yet: %s", exc)
        self._refresher = asyncio.create_task(self._refresh_loop())

    async def aclose(self) -> None:
        if self._refresher is not None:
            self._refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresher
        await self._client.aclose()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(ASSET_REFRESH_S if self._assets else ASSET_RETRY_S)
            try:
                await self.refresh_assets()
            except BrokerUnavailable as exc:
                log.warning("asset map refresh failed: %s", exc)

    async def refresh_assets(self) -> None:
        meta = await self._info({"type": "meta"})
        universe = meta.get("universe") if isinstance(meta, dict) else None
        if not isinstance(universe, list) or not universe:
            raise BrokerUnavailable("meta returned no universe")
        assets: dict[str, Asset] = {}
        # Delisted markets keep their index, so indices come from the unfiltered list.
        for index, entry in enumerate(universe):
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            assets[entry["name"]] = Asset(
                coin=entry["name"],
                index=index,
                sz_decimals=int(entry.get("szDecimals", 0)),
                max_leverage=int(entry.get("maxLeverage", 1)),
                delisted=bool(entry.get("isDelisted", False)),
            )
        self._assets = assets
        log.info("asset map: %d core perps", len(assets))

    def asset(self, symbol: str) -> Asset | None:
        if symbol in self._assets:
            return self._assets[symbol]
        matches = [a for name, a in self._assets.items() if name.upper() == symbol.upper()]
        return matches[0] if len(matches) == 1 else None

    async def _mid(self, coin: str) -> Decimal:
        try:
            mids = await self._info({"type": "allMids"})
        except BrokerUnavailable as exc:
            raise OrderRuleError(f"No market price for {coin} right now.", "not_ready") from exc
        raw = mids.get(coin) if isinstance(mids, dict) else None
        try:
            mid = Decimal(str(raw))
        except ArithmeticError:
            mid = Decimal(0)
        if not mid.is_finite() or mid <= 0:
            raise OrderRuleError(f"No market price for {coin} right now.", "not_ready")
        return mid

    # ── transport ───────────────────────────────────────────────────────────

    async def _info(self, body: dict[str, Any]) -> Any:
        try:
            response = await self._client.post("/info", json=body)
        except httpx.HTTPError as exc:
            raise BrokerUnavailable(
                f"Hyperliquid info unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code != 200:
            raise BrokerUnavailable(f"Hyperliquid info returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise BrokerUnavailable("Hyperliquid info returned invalid JSON") from None

    async def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST a signed request. Raises OutcomeUnknown when the venue may have
        acted on it, RequestRefused when it certainly did not."""
        try:
            response = await self._client.post("/exchange", json=request)
        except httpx.HTTPError as exc:
            # Includes timeouts after the body was sent: the order may be live.
            raise OutcomeUnknown(type(exc).__name__) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise OutcomeUnknown(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise RequestRefused(
                humanize_hl_error(f"HTTP {response.status_code}: {response.text[:300]}")
            )
        try:
            body = response.json()
        except ValueError:
            raise OutcomeUnknown("invalid JSON") from None
        if not isinstance(body, dict):
            raise OutcomeUnknown("unexpected response")
        if body.get("status") == "err":
            raw = body.get("response")
            raise RequestRefused(humanize_hl_error(raw if isinstance(raw, str) else ""))
        if body.get("status") != "ok":
            raise OutcomeUnknown("unexpected response")
        return body

    def _next_nonce(self) -> int:
        # Millisecond timestamps, strictly increasing within this process.
        self._last_nonce = max(int(self._clock() * 1000), self._last_nonce + 1)
        return self._last_nonce

    def _signed(self, action: dict[str, Any], expires_after: int | None) -> dict[str, Any]:
        nonce = self._next_nonce()
        signature = sign_l1_action(
            self._wallet, action, None, nonce, expires_after, self._is_mainnet
        )
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": expires_after,
        }

    # ── orders ──────────────────────────────────────────────────────────────

    async def prepare(self, intent: OrderIntent, cloid: str) -> PreparedOrder:
        if not self._assets:
            raise OrderRuleError("Market list not loaded yet; try again shortly.", "not_ready")
        asset = self.asset(intent.symbol)
        if asset is None:
            raise OrderRuleError(
                f"{intent.symbol} is not a Hyperliquid core perp market.", "unknown_market"
            )
        if asset.delisted:
            raise OrderRuleError(f"{asset.coin} is delisted.", "unknown_market")

        is_buy = intent.side.value == "buy"
        size = format_sz(intent.quantity, asset.sz_decimals)
        if Decimal(size) <= 0:
            step = Decimal(1).scaleb(-asset.sz_decimals)
            raise OrderRuleError(
                f"Size {intent.quantity} is below the smallest increment for {asset.coin} ({step})."
            )

        mid = await self._mid(asset.coin)
        band = Decimal(str(self._slippage))
        bps = self._settings.max_slippage_bps
        if intent.order_type is OrderType.MARKET:
            ref = Decimal(repr(intent.reference_price))
            if abs(ref - mid) > mid * band:
                raise OrderRuleError(
                    f"reference_price {intent.reference_price} is more than {bps} bps from the "
                    f"market mid {mid}; refresh the price and retry."
                )
            price = slippage_px(float(mid), is_buy, self._slippage, asset.sz_decimals)
            tif = "Ioc"
            notional_px = mid
        else:
            assert intent.limit_price is not None  # noqa: S101 - schema guarantees it
            price = format_px(intent.limit_price, asset.sz_decimals, "down" if is_buy else "up")
            px = Decimal(price)
            if (is_buy and px > mid * (1 + band)) or (not is_buy and px < mid * (1 - band)):
                raise OrderRuleError(
                    f"limit price {price} is more than {bps} bps through the market mid {mid}."
                )
            tif = "Gtc"
            notional_px = px

        if not intent.reduce_only and Decimal(size) * notional_px < MIN_ORDER_NOTIONAL_USD:
            minimum = min_size_for(float(notional_px), asset.sz_decimals)
            raise OrderRuleError(
                f"Order value is below Hyperliquid's $10 minimum. Use at least {minimum} "
                f"{asset.coin}.",
                "min_notional",
            )

        # Key order matters: the action is msgpack-hashed as sent (SDK order).
        wire = {
            "a": asset.index,
            "b": is_buy,
            "p": price,
            "s": size,
            "r": intent.reduce_only,
            "t": {"limit": {"tif": tif}},
            "c": cloid,
        }
        action = order_wires_to_order_action([wire])
        expires_at = int(self._clock() * 1000) + ORDER_TTL_MS
        request = self._signed(action, expires_at)
        return PreparedOrder(
            cloid=cloid,
            coin=asset.coin,
            asset=asset.index,
            request=request,
            expires_at_ms=expires_at,
        )

    async def send(self, intent: OrderIntent, order: PreparedOrder) -> OrderResult:
        assert order.request is not None  # noqa: S101
        body = await self._exchange(order.request)
        return self._order_result(intent.client_order_id, order.cloid, body, order.request)

    async def resend(self, row: LedgerRow) -> OrderResult:
        request = row.request
        if request is None:
            raise RequestRefused("no stored request to resend")
        body = await self._exchange(request)
        return self._order_result(row.client_order_id, row.cloid, body, request)

    def _order_result(
        self, client_order_id: str, cloid: str, body: dict[str, Any], request: dict[str, Any]
    ) -> OrderResult:
        statuses = _statuses(body)
        if len(statuses) != 1:
            raise OutcomeUnknown("no order status in the response")
        sent_size = _num(request["action"]["orders"][0]["s"])
        status = statuses[0]
        base = {"client_order_id": client_order_id, "broker": self.name, "cloid": cloid}
        if isinstance(status, dict):
            if isinstance(status.get("error"), str):
                return OrderResult(
                    **base, status=OrderStatus.REJECTED, detail=humanize_hl_error(status["error"])
                )
            filled = status.get("filled")
            if isinstance(filled, dict):
                total = _num(filled.get("totalSz"))
                detail = None if total >= sent_size else "partially filled; the rest was cancelled"
                return OrderResult(
                    **base,
                    status=OrderStatus.FILLED,
                    broker_order_id=str(filled.get("oid")),
                    filled_quantity=total,
                    average_price=_num(filled.get("avgPx")) or None,
                    detail=detail,
                )
            resting = status.get("resting")
            if isinstance(resting, dict):
                return OrderResult(
                    **base, status=OrderStatus.ACCEPTED, broker_order_id=str(resting.get("oid"))
                )
        if status in ("waitingForFill", "waitingForTrigger"):
            return OrderResult(**base, status=OrderStatus.ACCEPTED, detail=str(status))
        raise OutcomeUnknown("unrecognised order status")

    async def lookup(self, row: LedgerRow) -> OrderResult | None:
        body = await self._info({"type": "orderStatus", "user": self._user, "oid": row.cloid})
        if not isinstance(body, dict):
            raise BrokerUnavailable("unexpected orderStatus response")
        if body.get("status") == "unknownOid":
            return None
        entry = body.get("order")
        if body.get("status") != "order" or not isinstance(entry, dict):
            raise BrokerUnavailable("unexpected orderStatus response")
        order = entry.get("order") if isinstance(entry.get("order"), dict) else {}
        raw_status = str(entry.get("status", ""))
        status = status_from_order_status(raw_status)
        filled = max(0.0, _num(order.get("origSz")) - _num(order.get("sz")))
        return OrderResult(
            client_order_id=row.client_order_id,
            status=status,
            broker=self.name,
            broker_order_id=str(order["oid"]) if "oid" in order else row.broker_order_id,
            cloid=row.cloid,
            filled_quantity=filled,
            # orderStatus carries no average fill price; the ledger keeps any it had.
            average_price=None,
            detail=None if raw_status in ("open", "filled") else raw_status,
        )

    async def cancel(self, row: LedgerRow) -> OrderResult:
        if row.asset is None:
            raise BrokerUnavailable("order has no asset index")
        action = {"type": "cancelByCloid", "cancels": [{"asset": row.asset, "cloid": row.cloid}]}
        try:
            body = await self._exchange(self._signed(action, None))
        except (OutcomeUnknown, RequestRefused) as exc:
            raise BrokerUnavailable(f"cancel not confirmed: {exc}") from None
        statuses = _statuses(body)
        error = statuses[0].get("error") if statuses and isinstance(statuses[0], dict) else None
        found = await self.lookup(row)
        if found is None:
            return row.result().model_copy(update={"status": OrderStatus.PENDING})
        if error and found.status is OrderStatus.ACCEPTED:
            found = found.model_copy(update={"detail": humanize_hl_error(error)})
        return found

    async def cancel_all(self) -> CancelAllResult:
        orders = await self._info({"type": "openOrders", "user": self._user})
        if not isinstance(orders, list):
            raise BrokerUnavailable("unexpected openOrders response")
        if not self._assets:
            await self.refresh_assets()
        cancels: list[dict[str, int]] = []
        skipped: list[str] = []
        for order in orders:
            coin = order.get("coin") if isinstance(order, dict) else None
            asset = self._assets.get(coin) if isinstance(coin, str) else None
            if asset is None or "oid" not in order:
                skipped.append(str(coin))
                continue
            cancels.append({"a": asset.index, "o": int(order["oid"])})

        canceled = failed = 0
        errors: list[str] = []
        for start in range(0, len(cancels), CANCEL_BATCH):
            batch = cancels[start : start + CANCEL_BATCH]
            try:
                body = await self._exchange(
                    self._signed({"type": "cancel", "cancels": batch}, None)
                )
            except (OutcomeUnknown, RequestRefused) as exc:
                failed += len(batch)
                errors.append(redact_secrets(str(exc)))
                continue
            statuses = _statuses(body)
            if len(statuses) < len(batch):
                failed += len(batch) - len(statuses)
            for status in statuses:
                if status == "success":
                    canceled += 1
                else:
                    failed += 1
                    if isinstance(status, dict) and isinstance(status.get("error"), str):
                        errors.append(humanize_hl_error(status["error"]))
        return CancelAllResult(
            broker=self.name,
            canceled=canceled,
            failed=failed,
            skipped=skipped,
            detail="; ".join(dict.fromkeys(errors)) or None,
        )

    async def account(self) -> AccountState:
        state = await self._info({"type": "clearinghouseState", "user": self._user})
        if not isinstance(state, dict) or not isinstance(state.get("marginSummary"), dict):
            raise BrokerUnavailable("unexpected clearinghouseState response")
        summary = state["marginSummary"]
        positions = []
        for item in state.get("assetPositions") or []:
            pos = item.get("position") if isinstance(item, dict) else None
            if not isinstance(pos, dict):
                continue
            positions.append(
                Position(
                    symbol=str(pos.get("coin")),
                    size=_num(pos.get("szi")),
                    entry_price=_num(pos.get("entryPx")) or None,
                    position_value=_num(pos.get("positionValue")),
                    unrealized_pnl=_num(pos.get("unrealizedPnl")),
                )
            )
        # Equity drives the daily-loss limit: refuse to guess it.
        equity = _num(summary.get("accountValue"), math.nan)
        if not math.isfinite(equity):
            raise BrokerUnavailable("clearinghouseState has no usable accountValue")
        return AccountState(
            broker=self.name,
            equity=equity,
            margin_used=_num(summary.get("totalMarginUsed")),
            withdrawable=_num(state.get("withdrawable")),
            positions=positions,
        )
