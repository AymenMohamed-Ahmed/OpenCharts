from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.common.schemas import AccountState, CancelAllResult, OrderIntent, OrderResult
from app.execution.ledger import LedgerRow


class OutcomeUnknown(Exception):  # noqa: N818 - a state, not a failure of ours
    """The request may or may not have reached the venue. The ledger row stays
    `pending` and is reconciled by cloid."""


class RequestRefused(Exception):  # noqa: N818
    """The venue refused the whole request before acting on it (bad signature,
    nonce, unknown agent, malformed body). The message is safe to return."""


class BrokerUnavailable(Exception):  # noqa: N818
    """A read (account, status, market data) could not be completed."""


@dataclass(frozen=True)
class PreparedOrder:
    """An order ready to send: validated, priced, and (for a real venue) signed.

    `request` is the exact body that goes to the venue. It is stored in the
    ledger before sending and resent byte for byte while the outcome is unknown,
    so the venue's nonce check guarantees at most one order.
    """

    cloid: str
    coin: str
    asset: int | None
    request: dict[str, Any] | None
    expires_at_ms: int | None


class Broker(ABC):
    """One venue. Implementations hold the venue credentials."""

    name: str

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Load reference data and start background work."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""

    @abstractmethod
    async def prepare(self, intent: OrderIntent, cloid: str) -> PreparedOrder:
        """Validate and build the order. Sends nothing. Raises OrderRuleError."""

    @abstractmethod
    async def send(self, intent: OrderIntent, order: PreparedOrder) -> OrderResult:
        """First send. Raises OutcomeUnknown or RequestRefused."""

    @abstractmethod
    async def resend(self, row: LedgerRow) -> OrderResult:
        """Resend the stored request unchanged. Raises OutcomeUnknown or RequestRefused."""

    @abstractmethod
    async def lookup(self, row: LedgerRow) -> OrderResult | None:
        """Current state at the venue by cloid; None when the venue does not know
        it. Raises BrokerUnavailable."""

    @abstractmethod
    async def cancel(self, row: LedgerRow) -> OrderResult:
        """Cancel by cloid, then report the state. Raises BrokerUnavailable."""

    @abstractmethod
    async def cancel_all(self) -> CancelAllResult:
        """Cancel every open order this service trades. Raises BrokerUnavailable."""

    @abstractmethod
    async def account(self) -> AccountState:
        """Equity, margin and positions. Raises BrokerUnavailable."""
