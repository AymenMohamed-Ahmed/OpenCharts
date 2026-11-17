"""Order placement, idempotent on client_order_id and durable across restarts.

Flow for a new client_order_id, under a lock held for that id only:

  1. the broker validates, prices and signs the order (nothing is sent);
  2. the ledger records it as `pending`, with the exact signed request;
  3. the request is sent and the outcome recorded.

If the outcome is unknown (timeout, 5xx, crash between 2 and 3), the row stays
`pending`. Reconciliation asks the venue for the order by cloid. If the venue
does not know it and the signed request has not expired, the SAME request is
resent: its nonce can only be used once, so the venue cannot place it twice.
Once the request has expired and the venue still does not know the cloid, the
order was never placed and the row is closed as rejected.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from app.common.schemas import AccountState, CancelAllResult, OrderIntent, OrderResult, OrderStatus
from app.execution.broker.base import (
    Broker,
    BrokerUnavailable,
    OutcomeUnknown,
    PreparedOrder,
    RequestRefused,
)
from app.execution.broker.hl_rules import OrderRuleError
from app.execution.ledger import Ledger, LedgerRow, derive_cloid, intent_hash

log = logging.getLogger(__name__)

# How long past a signed request's expiry the venue may still be catching up
# (clock skew between this host and the venue).
EXPIRY_GRACE_MS = 60_000
RECONCILE_INTERVAL_S = 5.0


class IdempotencyConflict(Exception):  # noqa: N818
    """The client_order_id was already used for a different order."""


class OrderNotFound(Exception):  # noqa: N818
    pass


class OrderManager:
    def __init__(self, broker: Broker, ledger: Ledger, clock=time.time) -> None:
        self._broker = broker
        self._ledger = ledger
        self._clock = clock
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._reconciler: asyncio.Task[None] | None = None

    @property
    def broker_name(self) -> str:
        return self._broker.name

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    @asynccontextmanager
    async def _locked(self, client_order_id: str) -> AsyncIterator[None]:
        # One lock per id: a retry that arrives while the first attempt is in
        # flight waits for it; orders with other ids are not held up.
        lock, users = self._locks.get(client_order_id, (asyncio.Lock(), 0))
        self._locks[client_order_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[client_order_id]
            if users <= 1:
                del self._locks[client_order_id]
            else:
                self._locks[client_order_id] = (lock, users - 1)

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self, *, background: bool = True) -> None:
        """With `background`, pending rows are reconciled by a task that starts
        at once and repeats, so a slow venue cannot hold up start-up (and the
        health check). Without it, one pass runs inline (tests)."""
        await self._broker.start()
        if background:
            self._reconciler = asyncio.create_task(self._reconcile_loop())
        else:
            await self.reconcile_pending()

    async def aclose(self) -> None:
        if self._reconciler is not None:
            self._reconciler.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconciler
        await self._broker.aclose()
        self._ledger.close()

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_pending()
            except Exception as exc:
                log.error("reconciliation pass failed (%s)", type(exc).__name__)
            await asyncio.sleep(RECONCILE_INTERVAL_S)

    async def reconcile_pending(self) -> None:
        for row in self._ledger.pending(self._broker.venue):
            async with self._locked(row.client_order_id):
                current = self._ledger.get(row.client_order_id)
                if current is not None and current.status is OrderStatus.PENDING:
                    await self._reconcile(current)

    # ── orders ──────────────────────────────────────────────────────────────

    async def submit(self, intent: OrderIntent) -> OrderResult:
        async with self._locked(intent.client_order_id):
            row = self._ledger.get(intent.client_order_id)
            if row is not None:
                self._same_venue(row)
                if row.intent_hash != intent_hash(intent):
                    raise IdempotencyConflict(
                        f"client_order_id {intent.client_order_id} was already used for a "
                        "different order"
                    )
                if row.status is OrderStatus.PENDING:
                    return await self._reconcile(row)
                return row.result()

            cloid = derive_cloid(intent.client_order_id)
            try:
                prepared = await self._broker.prepare(intent, cloid)
            except OrderRuleError as exc:
                # Nothing was sent, so nothing is recorded: the caller may fix
                # the problem (or wait for market data) and retry with this id.
                return self._rejected(intent, cloid, str(exc))
            row = self._ledger.insert_pending(
                intent,
                cloid=cloid,
                broker=self._broker.name,
                venue=self._broker.venue,
                coin=prepared.coin,
                asset=prepared.asset,
                request=prepared.request,
                expires_at_ms=prepared.expires_at_ms,
            )
            return await self._first_send(row, intent, prepared)

    def _rejected(self, intent: OrderIntent, cloid: str, detail: str) -> OrderResult:
        return OrderResult(
            client_order_id=intent.client_order_id,
            status=OrderStatus.REJECTED,
            broker=self._broker.name,
            cloid=cloid,
            detail=detail,
        )

    async def _first_send(
        self, row: LedgerRow, intent: OrderIntent, prepared: PreparedOrder
    ) -> OrderResult:
        try:
            result = await self._broker.send(intent, prepared)
        except RequestRefused as exc:
            # The venue refused the request outright, so this nonce placed nothing.
            result = row.result().model_copy(
                update={"status": OrderStatus.REJECTED, "detail": str(exc)}
            )
        except Exception as exc:
            # OutcomeUnknown, or anything unexpected after the row was written:
            # the order may be live. Keep it pending; reconciliation decides.
            log.warning(
                "order %s: outcome unknown after send (%s); will reconcile",
                row.client_order_id,
                type(exc).__name__,
            )
            return self._pending(row)
        return self._ledger.record(row, result).result()

    def _pending(self, row: LedgerRow) -> OrderResult:
        return row.result().model_copy(
            update={
                "status": OrderStatus.PENDING,
                "detail": "outcome not known yet; retry with the same client_order_id to read it",
            }
        )

    async def _reconcile(self, row: LedgerRow) -> OrderResult:
        """Resolve a pending row. Caller holds the row's lock."""
        try:
            found = await self._broker.lookup(row)
        except Exception as exc:
            log.warning(
                "order %s: status lookup failed (%s)", row.client_order_id, type(exc).__name__
            )
            return self._pending(row)
        if found is not None:
            log.info("order %s: reconciled as %s", row.client_order_id, found.status.value)
            return self._ledger.record(row, found).result()

        # The venue does not know this cloid.
        now = self._now_ms()
        if row.expires_at_ms is not None and now < row.expires_at_ms + EXPIRY_GRACE_MS:
            # The first send may still land. Resend while the request is valid,
            # unless a cancel was asked for (which drops the stored request).
            if row.request is not None and now < row.expires_at_ms:
                return await self._resend(row)
            return self._pending(row)
        closed = row.result().model_copy(
            update={
                "status": OrderStatus.REJECTED,
                "detail": "not placed: the request never reached the exchange before it expired",
            }
        )
        log.info("order %s: closed as never placed", row.client_order_id)
        return self._ledger.record(row, closed).result()

    async def _resend(self, row: LedgerRow) -> OrderResult:
        try:
            result = await self._broker.resend(row)
        except (OutcomeUnknown, RequestRefused) as exc:
            # A refusal here is not final: the venue rejects a reused nonce when
            # the first send did land. The next lookup by cloid settles it.
            log.info(
                "order %s: resend not conclusive (%s)", row.client_order_id, type(exc).__name__
            )
            return self._pending(row)
        except Exception as exc:
            log.warning("order %s: resend failed (%s)", row.client_order_id, type(exc).__name__)
            return self._pending(row)
        return self._ledger.record(row, result).result()

    def _same_venue(self, row: LedgerRow) -> None:
        if row.venue != self._broker.venue:
            raise IdempotencyConflict(
                f"client_order_id {row.client_order_id} belongs to an order on {row.venue}, "
                f"not {self._broker.venue}"
            )

    async def status(self, client_order_id: str) -> OrderResult:
        async with self._locked(client_order_id):
            row = self._require(client_order_id)
            if row.venue != self._broker.venue:
                return row.result()  # recorded on another venue; nothing to ask here
            if row.status is OrderStatus.PENDING:
                return await self._reconcile(row)
            if row.status is OrderStatus.ACCEPTED:
                return await self._refresh(row)
            return row.result()

    async def _refresh(self, row: LedgerRow) -> OrderResult:
        try:
            found = await self._broker.lookup(row)
        except BrokerUnavailable:
            return row.result()
        if found is None:
            return row.result()
        return self._ledger.record(row, found).result()

    async def cancel(self, client_order_id: str) -> OrderResult:
        async with self._locked(client_order_id):
            row = self._require(client_order_id)
            if row.status.is_final:
                return row.result()
            self._same_venue(row)
            # Pending rows are cancelled too: if the order did land, the cancel
            # by cloid removes it.
            result = await self._broker.cancel(row)
            if result.status is OrderStatus.PENDING:
                # The venue does not know the order yet. Never resend it now;
                # reconciliation still records it if the first send lands.
                self._ledger.drop_request(row)
                return self._pending(row)
            return self._ledger.record(row, result).result()

    def _require(self, client_order_id: str) -> LedgerRow:
        row = self._ledger.get(client_order_id)
        if row is None:
            raise OrderNotFound(client_order_id)
        return row

    async def cancel_all(self) -> CancelAllResult:
        return await self._broker.cancel_all()

    async def account(self) -> AccountState:
        return await self._broker.account()
