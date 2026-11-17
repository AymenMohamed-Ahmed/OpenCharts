"""Durable order ledger (SQLite on the `execdata` volume).

A row is written as `pending` BEFORE the order is sent, with the exact signed
request, and updated with the outcome afterwards. After a crash or a timeout the
row is reconciled with the venue by `cloid`, so an order is never sent twice
and never forgotten.

Execution has no database network (plan ruling), so this file is the only
durable state the service keeps.
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.common.schemas import OrderIntent, OrderResult, OrderStatus

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    cloid           TEXT NOT NULL UNIQUE,
    intent_hash     TEXT NOT NULL,
    intent_json     TEXT NOT NULL,
    broker          TEXT NOT NULL,
    venue           TEXT NOT NULL,
    coin            TEXT NOT NULL,
    asset           INTEGER,
    status          TEXT NOT NULL,
    broker_order_id TEXT,
    filled_quantity REAL NOT NULL DEFAULT 0,
    average_price   REAL,
    detail          TEXT,
    request_json    TEXT,
    expires_at_ms   INTEGER,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status);
"""


def derive_cloid(client_order_id: str) -> str:
    """Hyperliquid client order id: the first 16 bytes of SHA-256 of the
    caller's id, as 0x-hex. Deterministic, so a retry or a restart finds the
    same order at the venue."""
    return "0x" + hashlib.sha256(client_order_id.encode()).digest()[:16].hex()


def intent_hash(intent: OrderIntent) -> str:
    canonical = json.dumps(intent.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class LedgerRow:
    client_order_id: str
    cloid: str
    intent_hash: str
    intent_json: str
    broker: str
    # Broker, network and account the order was sent to: a row is only ever
    # reconciled, resent or cancelled against the same venue.
    venue: str
    coin: str
    asset: int | None
    status: OrderStatus
    broker_order_id: str | None
    filled_quantity: float
    average_price: float | None
    detail: str | None
    request_json: str | None
    expires_at_ms: int | None
    created_at_ms: int
    updated_at_ms: int

    @property
    def request(self) -> dict[str, Any] | None:
        return json.loads(self.request_json) if self.request_json else None

    @property
    def intent(self) -> OrderIntent:
        return OrderIntent.model_validate_json(self.intent_json)

    def result(self) -> OrderResult:
        return OrderResult(
            client_order_id=self.client_order_id,
            status=self.status,
            broker=self.broker,
            broker_order_id=self.broker_order_id,
            cloid=self.cloid,
            filled_quantity=self.filled_quantity,
            average_price=self.average_price,
            detail=self.detail,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


class Ledger:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit; each write commits
        # before the call returns (synchronous=FULL), so a row survives a crash.
        # All access is from the event loop; check_same_thread=False only lets
        # test clients that run the app in another thread share it.
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        has_table = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        if has_table and version != SCHEMA_VERSION:
            self._db.close()
            raise RuntimeError(
                f"ledger {path} has schema version {version}, expected {SCHEMA_VERSION}; "
                "refusing to start rather than guess"
            )
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _row(r: sqlite3.Row | None) -> LedgerRow | None:
        if r is None:
            return None
        data = dict(r)
        data["status"] = OrderStatus(data["status"])
        return LedgerRow(**data)

    def get(self, client_order_id: str) -> LedgerRow | None:
        cur = self._db.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
        return self._row(cur.fetchone())

    def pending(self, venue: str) -> list[LedgerRow]:
        cur = self._db.execute(
            "SELECT * FROM orders WHERE status = ? AND venue = ? ORDER BY created_at_ms",
            (OrderStatus.PENDING.value, venue),
        )
        return [row for r in cur.fetchall() if (row := self._row(r)) is not None]

    def insert_pending(
        self,
        intent: OrderIntent,
        *,
        cloid: str,
        broker: str,
        venue: str,
        coin: str,
        asset: int | None,
        request: dict[str, Any] | None,
        expires_at_ms: int | None,
    ) -> LedgerRow:
        now = _now_ms()
        self._db.execute(
            "INSERT INTO orders (client_order_id, cloid, intent_hash, intent_json, broker, venue,"
            " coin, asset, status, request_json, expires_at_ms, created_at_ms, updated_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent.client_order_id,
                cloid,
                intent_hash(intent),
                intent.model_dump_json(),
                broker,
                venue,
                coin,
                asset,
                OrderStatus.PENDING.value,
                json.dumps(request) if request is not None else None,
                expires_at_ms,
                now,
                now,
            ),
        )
        row = self.get(intent.client_order_id)
        assert row is not None  # noqa: S101 - just inserted
        return row

    def record(self, row: LedgerRow, result: OrderResult) -> LedgerRow:
        """Store an outcome. The signed request is dropped once the outcome is
        final: it is only kept to resend while the outcome is unknown."""
        keep_request = result.status is OrderStatus.PENDING
        self._db.execute(
            "UPDATE orders SET status = ?, broker_order_id = ?, filled_quantity = ?,"
            " average_price = ?, detail = ?, request_json = ?, updated_at_ms = ?"
            " WHERE client_order_id = ?",
            (
                result.status.value,
                result.broker_order_id or row.broker_order_id,
                result.filled_quantity,
                result.average_price if result.average_price is not None else row.average_price,
                result.detail,
                row.request_json if keep_request else None,
                _now_ms(),
                row.client_order_id,
            ),
        )
        updated = self.get(row.client_order_id)
        assert updated is not None  # noqa: S101 - row exists
        return updated

    def drop_request(self, row: LedgerRow) -> None:
        """Forget the signed request so it is never resent (after a cancel)."""
        self._db.execute(
            "UPDATE orders SET request_json = NULL, updated_at_ms = ? WHERE client_order_id = ?",
            (_now_ms(), row.client_order_id),
        )
