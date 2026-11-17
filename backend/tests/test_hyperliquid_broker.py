"""Hyperliquid broker and order manager against the in-process fake exchange.

No test touches a network: the broker's HTTP client gets the fake as its
transport and never reads proxy settings from the environment.
"""

import asyncio
import logging

import pytest
from eth_account import Account

from app.common.config import ExecutionSettings
from app.common.schemas import OrderIntent, OrderStatus
from app.execution.broker.hl_errors import humanize_hl_error, redact_secrets
from app.execution.broker.hl_rules import slippage_px
from app.execution.broker.hyperliquid import HyperliquidBroker, load_agent_wallet
from app.execution.ledger import Ledger, derive_cloid
from app.execution.order_manager import EXPIRY_GRACE_MS, IdempotencyConflict, OrderManager
from tests.conftest import order
from tests.fake_hyperliquid import FakeHyperliquid

AGENT_KEY = "0x" + "4c" * 32
AGENT = Account.from_key(AGENT_KEY).address
USER = "0x" + "1a" * 20


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def settings(**overrides) -> ExecutionSettings:
    base = {
        "execution_internal_token": "t",
        "broker": "hyperliquid",
        "hl_account_address": USER,
        "hl_agent_private_key": AGENT_KEY,
    }
    return ExecutionSettings(**{**base, **overrides})


class Rig:
    def __init__(self, tmp_path, *, mainnet: bool = False) -> None:
        self.clock = Clock()
        self.tmp_path = tmp_path
        extra = {"hl_network": "mainnet", "live_trading": True} if mainnet else {}
        self.settings = settings(**extra)
        self.fake = FakeHyperliquid(
            agent_address=AGENT, user=USER, clock=self.clock, is_mainnet=mainnet
        )
        self.manager = self.new_manager()

    def new_manager(self) -> OrderManager:
        broker = HyperliquidBroker(self.settings, transport=self.fake, clock=self.clock)
        return OrderManager(broker, Ledger(self.tmp_path / "ledger.db"), clock=self.clock)

    async def start(self, manager: OrderManager | None = None) -> OrderManager:
        manager = manager or self.manager
        await manager.start(background=False)
        return manager


def run(coro):
    return asyncio.run(coro)


def intent(**overrides) -> OrderIntent:
    return OrderIntent(**order(**overrides))


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_market_order_is_signed_by_the_agent_and_priced_off_the_mid(rig):
    async def go():
        await rig.start()
        result = await rig.manager.submit(intent())
        await rig.manager.aclose()
        return result

    result = run(go())
    assert result.status is OrderStatus.FILLED
    assert result.cloid == derive_cloid("order-0001")
    (request,) = rig.fake.exchange_requests
    (wire,) = request["action"]["orders"]
    assert wire == {
        "a": 0,
        "b": True,
        "p": slippage_px(50000, True, 0.03, 5),
        "s": "0.01",
        "r": False,
        "t": {"limit": {"tif": "Ioc"}},
        "c": derive_cloid("order-0001"),
    }
    assert wire["p"] == "51500"
    assert request["expiresAfter"] > request["nonce"]
    assert result.filled_quantity == 0.01


def test_cloid_is_the_first_16_bytes_of_sha256():
    import hashlib

    expected = "0x" + hashlib.sha256(b"order-0001").hexdigest()[:32]
    assert derive_cloid("order-0001") == expected
    assert len(derive_cloid("x" * 64)) == 34


def test_mainnet_orders_are_signed_for_mainnet(tmp_path):
    rig = Rig(tmp_path, mainnet=True)

    async def go():
        await rig.start()
        return await rig.manager.submit(intent(side="sell"))

    result = run(go())
    assert result.status is OrderStatus.FILLED
    assert rig.fake.exchange_requests[0]["action"]["orders"][0]["p"] == "48500"


def test_limit_orders_rest_as_gtc_and_round_in_the_callers_favour(rig):
    async def go():
        await rig.start()
        buy = await rig.manager.submit(
            intent(
                client_order_id="limit-buy-1",
                symbol="ETH",
                quantity=0.01,
                order_type="limit",
                limit_price=2499.97,
                reference_price=None,
            )
        )
        sell = await rig.manager.submit(
            intent(
                client_order_id="limit-sell-1",
                symbol="ETH",
                side="sell",
                quantity=0.01,
                order_type="limit",
                limit_price=2500.01,
                reference_price=None,
            )
        )
        return buy, sell

    buy, sell = run(go())
    assert buy.status is sell.status is OrderStatus.ACCEPTED
    buy_wire = rig.fake.exchange_requests[0]["action"]["orders"][0]
    sell_wire = rig.fake.exchange_requests[1]["action"]["orders"][0]
    # ETH: szDecimals 4, so one decimal at this price. A buy rounds down, a sell up.
    assert (buy_wire["p"], buy_wire["t"]) == ("2499.9", {"limit": {"tif": "Gtc"}})
    assert sell_wire["p"] == "2500.1"
    assert buy.broker_order_id is not None


def test_symbols_match_case_insensitively_when_unambiguous(rig):
    async def go():
        await rig.start()
        rig.fake.mids["kPEPE"] = "0.02"
        return await rig.manager.submit(intent(symbol="KPEPE", quantity=1000, reference_price=0.02))

    assert run(go()).status is OrderStatus.FILLED
    assert rig.fake.exchange_requests[0]["action"]["orders"][0]["a"] == 3


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbol": "DOGE"}, "not a Hyperliquid core perp"),
        ({"symbol": "OLD"}, "delisted"),
        ({"quantity": 0.0001}, "$10 minimum"),
        ({"quantity": 0.000001}, "smallest increment"),
        ({"reference_price": 40000}, "bps from the market mid"),
        (
            {"order_type": "limit", "limit_price": 52000, "reference_price": None},
            "bps through the market mid",
        ),
        (
            {"side": "sell", "order_type": "limit", "limit_price": 48000, "reference_price": None},
            "bps through the market mid",
        ),
    ],
)
def test_orders_the_venue_would_refuse_are_never_sent(rig, overrides, message):
    async def go():
        await rig.start()
        first = await rig.manager.submit(intent(**overrides))
        # Not recorded: the same id can be used again once the order is fixed.
        again = await rig.manager.submit(intent(**overrides))
        return first, again

    first, again = run(go())
    assert first.status is OrderStatus.REJECTED
    assert message in first.detail
    assert again == first
    assert rig.fake.exchange_requests == []


def test_reduce_only_orders_are_exempt_from_the_minimum(rig):
    async def go():
        await rig.start()
        return await rig.manager.submit(intent(quantity=0.0001, reduce_only=True, side="sell"))

    assert run(go()).status is OrderStatus.FILLED
    assert rig.fake.exchange_requests[0]["action"]["orders"][0]["r"] is True


def test_no_mid_or_no_market_list_refuses_the_order(rig):
    async def go():
        rig.fake.info_down = True
        await rig.start()
        first = await rig.manager.submit(intent())
        rig.fake.info_down = False
        await rig.manager._broker.refresh_assets()
        rig.fake.mids.pop("BTC")
        second = await rig.manager.submit(intent())
        return first, second

    first, second = run(go())
    assert "not loaded" in first.detail
    assert "No market price" in second.detail
    assert rig.fake.exchange_requests == []


def test_exchange_rejection_is_recorded_and_not_resent(rig):
    rig.fake.reject_next_order = "Insufficient margin to place order. asset=0"

    async def go():
        await rig.start()
        first = await rig.manager.submit(intent())
        second = await rig.manager.submit(intent())
        return first, second

    first, second = run(go())
    assert first.status is OrderStatus.REJECTED
    assert "Not enough margin" in first.detail
    assert second == first
    assert len(rig.fake.exchange_requests) == 1


def test_top_level_refusal_is_a_rejection(rig):
    rig.fake.refuse_next_request = "User or API Wallet 0xabc does not exist."

    async def go():
        await rig.start()
        return await rig.manager.submit(intent())

    result = run(go())
    assert result.status is OrderStatus.REJECTED
    assert "not authorised" in result.detail


def test_lost_response_is_pending_then_reconciled_without_a_second_order(rig):
    rig.fake.lose_next_response = 1

    async def go():
        await rig.start()
        first = await rig.manager.submit(intent())
        retry = await rig.manager.submit(intent())
        return first, retry

    first, retry = run(go())
    assert first.status is OrderStatus.PENDING
    assert retry.status is OrderStatus.FILLED
    assert retry.filled_quantity == 0.01
    assert len(rig.fake.exchange_requests) == 1
    assert len(rig.fake.orders) == 1


def test_request_that_never_arrived_is_resent_byte_for_byte(rig):
    rig.fake.drop_next_request = 1

    async def go():
        await rig.start()
        first = await rig.manager.submit(intent())
        await rig.manager.reconcile_pending()
        return first, await rig.manager.status("order-0001")

    first, after = run(go())
    assert first.status is OrderStatus.PENDING
    assert after.status is OrderStatus.FILLED
    (request,) = rig.fake.exchange_requests
    assert len(rig.fake.orders) == 1
    # The ledger keeps no signed request once the outcome is final.
    assert rig.manager._ledger.get("order-0001").request is None
    assert request["nonce"] in rig.fake.used_nonces


def test_resend_after_the_first_landed_is_refused_by_nonce_and_stays_safe(rig):
    async def go():
        await rig.start()
        rig.fake.lose_next_response = 1
        await rig.manager.submit(intent())
        row = rig.manager._ledger.get("order-0001")
        # Resend the stored request directly: the venue refuses the reused nonce.
        await rig.manager._resend(row)
        return await rig.manager.status("order-0001")

    after = run(go())
    assert after.status is OrderStatus.FILLED
    assert len(rig.fake.orders) == 1
    assert len(rig.fake.exchange_requests) == 2


def test_crash_between_ledger_write_and_send_is_recovered_on_start(rig):
    async def crash():
        await rig.start()
        prepared = await rig.manager._broker.prepare(intent(), derive_cloid("order-0001"))
        rig.manager._ledger.insert_pending(
            intent(),
            cloid=prepared.cloid,
            broker="hyperliquid",
            venue=rig.manager._broker.venue,
            coin=prepared.coin,
            asset=prepared.asset,
            request=prepared.request,
            expires_at_ms=prepared.expires_at_ms,
        )
        # The process dies here, before sending.
        rig.manager._ledger.close()

    async def restart():
        manager = rig.new_manager()
        await rig.start(manager)
        return manager._ledger.get("order-0001")

    run(crash())
    row = run(restart())
    assert row.status is OrderStatus.FILLED
    assert len(rig.fake.orders) == 1


def test_expired_unknown_order_is_closed_as_never_placed(rig):
    rig.fake.drop_next_request = 1

    async def go():
        await rig.start()
        await rig.manager.submit(intent())
        rig.fake.drop_next_request = 10
        rig.clock.t += 31  # past expiry, inside the grace period
        in_grace = await rig.manager.status("order-0001")
        sent_in_grace = len(rig.fake.exchange_requests)
        rig.clock.t += EXPIRY_GRACE_MS / 1000
        closed = await rig.manager.status("order-0001")
        return in_grace, sent_in_grace, closed

    in_grace, sent_in_grace, closed = run(go())
    assert in_grace.status is OrderStatus.PENDING
    assert sent_in_grace == 0  # expired requests are not resent
    assert closed.status is OrderStatus.REJECTED
    assert "never reached the exchange" in closed.detail
    assert rig.fake.orders == {}


def test_concurrent_retries_place_one_order(rig):
    rig.fake.exchange_delay_s = 0.05

    async def go():
        await rig.start()
        return await asyncio.gather(*(rig.manager.submit(intent()) for _ in range(8)))

    results = run(go())
    assert len(rig.fake.exchange_requests) == 1
    assert all(r == results[0] for r in results)
    assert results[0].status is OrderStatus.FILLED


def test_different_ids_are_not_serialised(rig):
    rig.fake.exchange_delay_s = 0.05

    async def go():
        await rig.start()
        await asyncio.gather(
            *(rig.manager.submit(intent(client_order_id=f"parallel-{i:03}")) for i in range(4))
        )

    run(go())
    assert rig.fake.max_in_flight > 1
    assert len(rig.fake.orders) == 4
    nonces = [r["nonce"] for r in rig.fake.exchange_requests]
    assert len(set(nonces)) == 4


def test_reusing_an_id_for_a_different_order_is_a_conflict(rig):
    async def go():
        await rig.start()
        await rig.manager.submit(intent())
        await rig.manager.submit(intent(quantity=0.02))

    with pytest.raises(IdempotencyConflict):
        run(go())
    assert len(rig.fake.exchange_requests) == 1


def test_cancel_by_cloid(rig):
    async def go():
        await rig.start()
        await rig.manager.submit(
            intent(order_type="limit", limit_price=49000, reference_price=None)
        )
        canceled = await rig.manager.cancel("order-0001")
        again = await rig.manager.cancel("order-0001")
        return canceled, again

    canceled, again = run(go())
    assert canceled.status is OrderStatus.CANCELED
    assert again == canceled
    cancel = rig.fake.exchange_requests[1]["action"]
    assert cancel == {
        "type": "cancelByCloid",
        "cancels": [{"asset": 0, "cloid": derive_cloid("order-0001")}],
    }
    assert len(rig.fake.exchange_requests) == 2  # a final order is not cancelled again


def test_cancel_of_a_pending_order_stops_resends(rig):
    rig.fake.drop_next_request = 1

    async def go():
        await rig.start()
        await rig.manager.submit(intent())
        canceled = await rig.manager.cancel("order-0001")
        await rig.manager.reconcile_pending()
        return canceled

    canceled = run(go())
    assert canceled.status is OrderStatus.PENDING
    assert rig.fake.orders == {}
    assert [r["action"]["type"] for r in rig.fake.exchange_requests] == ["cancelByCloid"]


def test_status_refreshes_resting_orders(rig):
    async def go():
        await rig.start()
        await rig.manager.submit(
            intent(order_type="limit", limit_price=49000, reference_price=None)
        )
        order_ = rig.fake.orders[derive_cloid("order-0001")]
        order_["status"], order_["remaining"] = "filled", "0.0"
        return await rig.manager.status("order-0001")

    result = run(go())
    assert result.status is OrderStatus.FILLED
    assert result.filled_quantity == 0.01


def test_cancel_all_cancels_core_perps_and_skips_the_rest(rig):
    rig.fake.extra_open_orders = [
        {"coin": "ETH", "oid": 77},
        {"coin": "@107", "oid": 78},
        {"coin": "xyz:TSLA", "oid": 79},
    ]

    async def go():
        await rig.start()
        await rig.manager.submit(
            intent(order_type="limit", limit_price=49000, reference_price=None)
        )
        return await rig.manager.cancel_all()

    result = run(go())
    assert (result.canceled, result.failed) == (2, 0)
    assert sorted(result.skipped) == ["@107", "xyz:TSLA"]
    action = rig.fake.exchange_requests[-1]["action"]
    assert action["type"] == "cancel"
    assert {"a": 1, "o": 77} in action["cancels"]


def test_account_state(rig):
    async def go():
        await rig.start()
        return await rig.manager.account()

    account = run(go())
    assert account.equity == 1000.0
    assert account.margin_used == 12.5
    assert account.positions[0].symbol == "BTC"
    assert account.positions[0].size == -0.01


def test_unusable_equity_is_an_error_not_a_guess(rig):
    from app.execution.broker.base import BrokerUnavailable

    rig.fake.account_value = "NaN"

    async def go():
        await rig.start()
        return await rig.manager.account()

    with pytest.raises(BrokerUnavailable):
        run(go())


def test_nonces_strictly_increase_on_a_frozen_clock(rig):
    broker = rig.manager._broker
    nonces = [broker._next_nonce() for _ in range(5)]
    assert nonces == sorted(set(nonces))


def test_the_key_never_reaches_logs_or_responses(rig, caplog):
    caplog.set_level(logging.DEBUG)
    rig.fake.refuse_next_request = f"bad signature from key {AGENT_KEY}"

    async def go():
        await rig.start()
        rejected = await rig.manager.submit(intent())
        rig.fake.lose_next_response = 1
        pending = await rig.manager.submit(intent(client_order_id="order-0002"))
        return rejected, pending

    rejected, pending = run(go())
    bare = AGENT_KEY.removeprefix("0x")
    assert bare not in rejected.model_dump_json()
    assert bare not in pending.model_dump_json()
    assert bare not in caplog.text
    assert bare not in repr(rig.settings)


def test_wallet_loading_errors_never_include_the_key():
    secret = "0x" + "zz" * 32
    with pytest.raises(ValueError) as excinfo:
        load_agent_wallet(settings(hl_agent_private_key=secret))
    assert "zz" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_the_account_key_itself_is_refused_as_agent_key():
    with pytest.raises(ValueError, match="API \\(agent\\) wallet"):
        load_agent_wallet(settings(hl_account_address=AGENT))


def test_redaction():
    sig = "0x" + "ab" * 65
    assert redact_secrets(f"bad sig {sig}") == "bad sig [redacted]"
    assert "abab" not in humanize_hl_error(f"weird {sig}")
    assert "4c4c" not in redact_secrets(AGENT_KEY.removeprefix("0x"))
    assert humanize_hl_error("") == "Hyperliquid rejected the order."
    message = humanize_hl_error("Some brand new failure " + "x" * 500)
    assert message.startswith("Hyperliquid rejected the order: Some brand new failure")
    assert len(message) < 260


def test_rows_from_another_network_or_account_are_left_alone(tmp_path):
    testnet = Rig(tmp_path)
    testnet.fake.drop_next_request = 1

    async def on_testnet():
        await testnet.start()
        await testnet.manager.submit(intent())  # pending on testnet
        await testnet.manager.submit(
            intent(
                client_order_id="resting-0001",
                order_type="limit",
                limit_price=49000,
                reference_price=None,
            )
        )
        testnet.manager._ledger.close()

    run(on_testnet())
    mainnet = Rig(tmp_path, mainnet=True)

    async def on_mainnet():
        await mainnet.start()  # reconciles only mainnet rows
        status = await mainnet.manager.status("order-0001")
        with pytest.raises(IdempotencyConflict, match="hyperliquid-testnet"):
            await mainnet.manager.submit(intent())
        with pytest.raises(IdempotencyConflict):
            await mainnet.manager.cancel("resting-0001")
        return status

    status = run(on_mainnet())
    assert status.status is OrderStatus.PENDING
    assert mainnet.fake.exchange_requests == []
    assert [i["type"] for i in mainnet.fake.info_requests] == ["meta"]


def test_cancel_all_stops_resending_pending_orders(rig):
    rig.fake.drop_next_request = 1

    async def go():
        await rig.start()
        first = await rig.manager.submit(intent())
        await rig.manager.cancel_all()
        await rig.manager.reconcile_pending()
        return first

    assert run(go()).status is OrderStatus.PENDING
    assert rig.fake.orders == {}
    assert rig.manager._ledger.get("order-0001").request is None
