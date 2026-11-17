"""In-process fake of the Hyperliquid /info and /exchange endpoints.

It checks every signed request the way the venue does: it recomputes the L1
action hash independently (msgpack, nonce, vault flag, expiresAfter), recovers
the signer with eth_account and requires it to be the agent wallet, and refuses
a reused nonce or an expired request. Orders are kept by cloid so tests can
assert that a retry never places a second one.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import msgpack
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

UNIVERSE = [
    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
    {"name": "OLD", "szDecimals": 2, "maxLeverage": 3, "isDelisted": True},
    {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
]


def recover_l1_signer(request: dict[str, Any], is_mainnet: bool) -> str:
    action = request["action"]
    data = msgpack.packb(action)
    data += request["nonce"].to_bytes(8, "big")
    assert request["vaultAddress"] is None
    data += b"\x00"
    if request.get("expiresAfter") is not None:
        data += b"\x00" + request["expiresAfter"].to_bytes(8, "big")
    typed = {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "Agent",
        "message": {"source": "a" if is_mainnet else "b", "connectionId": keccak(data)},
    }
    sig = request["signature"]
    return Account.recover_message(
        encode_typed_data(full_message=typed), vrs=[sig["v"], sig["r"], sig["s"]]
    )


@dataclass
class FakeHyperliquid(httpx.AsyncBaseTransport):
    agent_address: str
    user: str
    clock: Any
    is_mainnet: bool = False
    universe: list[dict[str, Any]] = field(default_factory=lambda: [dict(u) for u in UNIVERSE])
    mids: dict[str, str] = field(default_factory=lambda: {"BTC": "50000", "ETH": "2500"})
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    exchange_requests: list[dict[str, Any]] = field(default_factory=list)
    info_requests: list[dict[str, Any]] = field(default_factory=list)
    used_nonces: set[int] = field(default_factory=set)
    # Hooks a test sets for the next /exchange call(s).
    lose_next_response: int = 0  # process, then fail as a read timeout
    drop_next_request: int = 0  # fail as a connect error, never processed
    reject_next_order: str | None = None  # per-order error text
    refuse_next_request: str | None = None  # top-level {"status": "err"}
    exchange_delay_s: float = 0.0
    info_down: bool = False
    in_flight: int = 0
    max_in_flight: int = 0
    resting: bool = True  # IOC orders fill; GTC orders rest unless this is False
    extra_open_orders: list[dict[str, Any]] = field(default_factory=list)
    account_value: str = "1000.0"
    _next_oid: int = 1000

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/info":
            self.info_requests.append(body)
            if self.info_down:
                return httpx.Response(503)
            return httpx.Response(200, json=self._info(body))
        assert request.url.path == "/exchange"
        if self.drop_next_request:
            self.drop_next_request -= 1
            raise httpx.ConnectError("connection refused", request=request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.exchange_delay_s:
                await asyncio.sleep(self.exchange_delay_s)
            response = self._exchange(body)
        finally:
            self.in_flight -= 1
        if self.lose_next_response:
            self.lose_next_response -= 1
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=response)

    # ── /info ───────────────────────────────────────────────────────────────

    def _info(self, body: dict[str, Any]) -> Any:
        kind = body["type"]
        if kind == "meta":
            return {"universe": self.universe}
        if kind == "allMids":
            return self.mids
        if kind == "orderStatus":
            assert body["user"] == self.user
            order = self.orders.get(body["oid"])
            if order is None:
                return {"status": "unknownOid"}
            return {
                "status": "order",
                "order": {
                    "order": {
                        "coin": order["coin"],
                        "side": "B" if order["is_buy"] else "A",
                        "limitPx": order["p"],
                        "sz": order["remaining"],
                        "oid": order["oid"],
                        "origSz": order["s"],
                        "cloid": order["cloid"],
                    },
                    "status": order["status"],
                    "statusTimestamp": 0,
                },
            }
        if kind == "openOrders":
            assert body["user"] == self.user
            mine = [
                {"coin": o["coin"], "oid": o["oid"], "side": "B", "sz": o["remaining"]}
                for o in self.orders.values()
                if o["status"] == "open"
            ]
            return mine + self.extra_open_orders
        if kind == "clearinghouseState":
            assert body["user"] == self.user
            return {
                "marginSummary": {"accountValue": self.account_value, "totalMarginUsed": "12.5"},
                "withdrawable": "900.0",
                "assetPositions": [
                    {
                        "type": "oneWay",
                        "position": {
                            "coin": "BTC",
                            "szi": "-0.01",
                            "entryPx": "50100.0",
                            "positionValue": "500.0",
                            "unrealizedPnl": "1.0",
                        },
                    }
                ],
            }
        raise AssertionError(f"unexpected info type {kind}")

    # ── /exchange ───────────────────────────────────────────────────────────

    def _exchange(self, body: dict[str, Any]) -> dict[str, Any]:
        self.exchange_requests.append(body)
        signer = recover_l1_signer(body, self.is_mainnet)
        assert signer == self.agent_address, "request not signed by the agent wallet"
        now_ms = int(self.clock() * 1000)
        if body.get("expiresAfter") is not None and now_ms > body["expiresAfter"]:
            return {"status": "err", "response": "Action expired"}
        if body["nonce"] in self.used_nonces:
            return {"status": "err", "response": "Nonce already used"}
        self.used_nonces.add(body["nonce"])
        if self.refuse_next_request:
            message, self.refuse_next_request = self.refuse_next_request, None
            return {"status": "err", "response": message}
        action = body["action"]
        if action["type"] == "order":
            return self._order(action)
        if action["type"] == "cancelByCloid":
            statuses = []
            for cancel in action["cancels"]:
                order = self.orders.get(cancel["cloid"])
                if order and order["status"] == "open" and order["asset"] == cancel["asset"]:
                    order["status"] = "canceled"
                    statuses.append("success")
                else:
                    statuses.append(
                        {"error": "Order was never placed, already canceled, or filled."}
                    )
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": statuses}}}
        if action["type"] == "cancel":
            statuses = []
            by_oid = {o["oid"]: o for o in self.orders.values()}
            for cancel in action["cancels"]:
                order = by_oid.get(cancel["o"])
                extra = any(o["oid"] == cancel["o"] for o in self.extra_open_orders)
                if order and order["status"] == "open":
                    order["status"] = "canceled"
                    statuses.append("success")
                elif extra:
                    statuses.append("success")
                else:
                    statuses.append(
                        {"error": "Order was never placed, already canceled, or filled."}
                    )
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": statuses}}}
        raise AssertionError(f"unexpected action {action['type']}")

    def _order(self, action: dict[str, Any]) -> dict[str, Any]:
        assert action["grouping"] == "na"
        assert "builder" not in action
        statuses = []
        for wire in action["orders"]:
            assert list(wire) == ["a", "b", "p", "s", "r", "t", "c"]
            assert isinstance(wire["p"], str) and isinstance(wire["s"], str)
            if self.reject_next_order:
                message, self.reject_next_order = self.reject_next_order, None
                statuses.append({"error": message})
                continue
            if wire["c"] in self.orders:
                statuses.append({"error": "Duplicate cloid"})
                continue
            self._next_oid += 1
            tif = wire["t"]["limit"]["tif"]
            rests = tif == "Gtc" and self.resting
            order = {
                "oid": self._next_oid,
                "asset": wire["a"],
                "coin": self.universe[wire["a"]]["name"],
                "is_buy": wire["b"],
                "p": wire["p"],
                "s": wire["s"],
                "remaining": wire["s"] if rests else "0.0",
                "cloid": wire["c"],
                "status": "open" if rests else "filled",
            }
            self.orders[wire["c"]] = order
            if rests:
                statuses.append({"resting": {"oid": order["oid"], "cloid": wire["c"]}})
            else:
                statuses.append(
                    {"filled": {"oid": order["oid"], "totalSz": wire["s"], "avgPx": wire["p"]}}
                )
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
