import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.db import transaction
from test_wallet_transactions import wallet_actor as wallet_actor

from apps.common.exceptions import DomainError
from apps.markets.models import Instrument, PriceObservation, ProviderHealth, Quote
from apps.trading.dtos import CancelOrderCommand, ExecuteOrderCommand, PlaceOrderCommand
from apps.trading.models import AssetMovement, CostLot, Fill, Order, Position
from apps.trading.selectors import TradingSelector
from apps.trading.services import (
    AccessRevocation,
    CancelOrder,
    ExecuteOrder,
    OrderMatcher,
    PlaceOrder,
)
from apps.wallets.dtos import FundingCommand, ReversalCommand
from apps.wallets.models import Journal, Wallet
from apps.wallets.services import ReverseFunding, TopUpWallet, WithdrawFunds
from apps.wallets.services.reconciliation import Reconciliation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def clock(monkeypatch):
    clock = {"now": datetime(2026, 5, 18, 10, tzinfo=UTC)}
    monkeypatch.setattr("django.utils.timezone.now", lambda: clock["now"])
    return clock


@pytest.fixture
def instrument(clock):
    instrument = Instrument.objects.create(symbol="BTCUSDT", name="Bitcoin", market="CRYPTO",
        currency="USDT", provider="test-source", provider_symbol="BTCUSDT", price_scale=2,
        quantity_scale=3, quantity_step=1, minimum_quantity=1, minimum_notional=100000000,
        enabled=True, data_mode="LIVE", delay_seconds=0, cadence_seconds=10)
    generation = uuid.uuid4()
    ProviderHealth.objects.create(provider=instrument.provider, status="AVAILABLE", generation=generation, last_success_at=clock["now"])
    Quote.objects.create(instrument=instrument, price_units=10000,
        source_at=clock["now"] - timedelta(seconds=1), received_at=clock["now"],
        observation_id="initial", generation=generation, tradable=True)
    return instrument


@pytest.fixture
def publish(instrument, clock):
    def publish(price=9000, *, seconds=2, **overrides):
        clock["now"] += timedelta(seconds=seconds)
        values = {"price_units": price, "source_at": clock["now"] - timedelta(milliseconds=100),
                  "received_at": clock["now"], "observation_id": str(uuid.uuid4()), "tradable": True,
                  **overrides}
        Quote.objects.filter(instrument=instrument).update(**values)
        ProviderHealth.objects.filter(provider=instrument.provider).update(last_success_at=clock["now"])
        quote = Quote.objects.get(instrument=instrument)
        if instrument.data_mode == "EOD":
            PriceObservation.objects.create(instrument=instrument, kind="BAR", price_units=quote.price_units,
                source_at=quote.source_at, received_at=quote.received_at,
                observation_id=quote.observation_id, generation=quote.generation, tradable=quote.tradable)
        return quote
    return publish


def fund(actor, amount="100", currency="USDT", key="fund"):
    return TopUpWallet().execute(actor, FundingCommand(currency, amount), key)


def buy(actor, instrument, *, budget="100", key="buy"):
    return PlaceOrder().execute(actor, PlaceOrderCommand(str(instrument.pk), "BUY", "MARKET", budget=budget), key)


def limit(actor, instrument, side="BUY", quantity="1.000", price="100.00", key="limit"):
    return PlaceOrder().execute(actor, PlaceOrderCommand(str(instrument.pk), side, "LIMIT", quantity=quantity, limit_price=price), key)


def execute(order):
    return ExecuteOrder().execute(ExecuteOrderCommand(order.id))


def test_market_budget_reservation_floor_refund_and_idempotent_fill(wallet_actor, instrument, publish):
    funding = fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    assert buy(wallet_actor, instrument).id == order.id
    wallet = Wallet.objects.get(user=wallet_actor)
    assert (wallet.available_units, wallet.reserved_units) == (0, 10000000000)
    assert execute(order) is None
    for action in (
        lambda: WithdrawFunds().execute(wallet_actor, FundingCommand("USDT", "1"), "withdraw"),
        lambda: ReverseFunding().execute(wallet_actor, ReversalCommand(funding.id, "Correction"), "reverse"),
    ):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.code == "insufficient_funds"
    publish(9000)
    fill = execute(order)
    assert fill.quantity == "1.111"
    assert fill.cash == "99.99000000"
    assert fill.fees == "0.00000000"
    assert execute(order) == fill
    wallet.refresh_from_db()
    assert (wallet.available_units, wallet.reserved_units) == (1000000, 0)
    assert Fill.objects.count() == 1
    assert CostLot.objects.get().remaining_quantity_units == 1111
    assert Reconciliation.run() == ()


def test_limit_buy_price_improvement_and_fifo_sale(wallet_actor, instrument, publish):
    fund(wallet_actor, "200")
    order = limit(wallet_actor, instrument)
    publish(10100)
    assert execute(order) is None
    publish(9000)
    assert execute(order).cash == "90.00000000"
    second = limit(wallet_actor, instrument, key="limit2")
    publish(9500)
    execute(second)
    sell = limit(wallet_actor, instrument, side="SELL", quantity="1.500", price="100.00", key="sell")
    position = Position.objects.get(user=wallet_actor)
    assert (position.available_units, position.reserved_units) == (500, 1500)
    publish(11000)
    fill = execute(sell)
    assert fill.cash == "165.00000000"
    assert fill.cost_basis == "137.50000000"
    assert fill.realized_pnl == "27.50000000"
    position.refresh_from_db()
    assert position.cost_basis_units == 4750000000
    assert position.realized_pnl_units == 2750000000
    portfolio = TradingSelector.portfolio(wallet_actor)
    assert portfolio[0].currency == "USDT"
    assert portfolio[0].unrealized_pnl == "7.50000000"
    assert portfolio[0].realized_pnl == "27.50000000"
    assert not portfolio[0].stale
    assert Reconciliation.run() == ()


def test_sell_requires_owned_available_quantity_and_cancel_releases(wallet_actor, instrument, publish):
    with pytest.raises(DomainError) as error:
        limit(wallet_actor, instrument, side="SELL")
    assert error.value.code == "insufficient_holdings"
    assert not Order.objects.exists()
    fund(wallet_actor)
    acquired = limit(wallet_actor, instrument)
    publish(10000)
    execute(acquired)
    sale = limit(wallet_actor, instrument, side="SELL", key="sale")
    with pytest.raises(DomainError):
        limit(wallet_actor, instrument, side="SELL", key="double-sale")
    result = CancelOrder().execute(wallet_actor, CancelOrderCommand(sale.id), "cancel")
    assert result.status == "CANCELLED"
    assert CancelOrder().execute(wallet_actor, CancelOrderCommand(sale.id), "cancel") == result
    assert Position.objects.get(user=wallet_actor).reserved_units == 0
    assert AssetMovement.objects.filter(order_id=sale.id).count() == 2
    publish(10000)
    assert execute(sale) is None
    assert Reconciliation.run() == ()


@pytest.mark.parametrize("bad", ["old_source", "future_source", "old_received", "same_watermark", "generation", "backfill", "unhealthy"])
def test_ineligible_quotes_never_fill(wallet_actor, instrument, publish, clock, bad):
    fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    accepted = Order.objects.get(pk=order.id).accepted_at
    overrides = {}
    if bad == "old_source":
        overrides["source_at"] = accepted
    elif bad == "future_source":
        overrides["source_at"] = accepted + timedelta(seconds=10)
    elif bad == "old_received":
        overrides.update(source_at=accepted + timedelta(milliseconds=100), received_at=accepted)
    elif bad == "same_watermark":
        overrides["observation_id"] = "initial"
    elif bad == "generation":
        overrides["generation"] = uuid.uuid4()
    elif bad == "backfill":
        overrides["tradable"] = False
    publish(**overrides)
    if bad == "unhealthy":
        ProviderHealth.objects.update(status="DEGRADED")
    assert execute(order) is None
    assert not Fill.objects.exists()
    assert Order.objects.get(pk=order.id).status == "OPEN"


def test_stale_mark_and_stale_fill_rejection(wallet_actor, instrument, publish, clock):
    fund(wallet_actor, "200")
    order = limit(wallet_actor, instrument)
    publish(9000)
    execute(order)
    pending = limit(wallet_actor, instrument, key="pending")
    publish(9000)
    clock["now"] += timedelta(seconds=40)
    assert execute(pending) is None
    portfolio = TradingSelector.portfolio(wallet_actor)
    assert portfolio[0].stale
    assert portfolio[0].holdings[0].stale
    assert portfolio[0].holdings[0].mark_price == "90.00"


def test_market_timeout_and_immutable_terminal_state(wallet_actor, instrument, clock):
    fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    clock["now"] += timedelta(seconds=60)
    assert OrderMatcher().run() == 0
    closed = Order.objects.get(pk=order.id)
    assert closed.status == "EXPIRED"
    assert Wallet.objects.get(user=wallet_actor).reserved_units == 0
    with pytest.raises(DomainError):
        CancelOrder().execute(wallet_actor, CancelOrderCommand(order.id), "cancel-expired")
    with pytest.raises(DomainError):
        closed.status = "OPEN"
        closed.save()
    assert Reconciliation.run() == ()


def test_revocation_releases_without_cancel_permission_and_blocks_replay(wallet_actor, instrument):
    fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    with transaction.atomic():
        wallet_actor.groups.clear()
        assert AccessRevocation.cancel_unauthorized([wallet_actor.pk]) == 1
        assert AccessRevocation.cancel_unauthorized() == 0
    assert Order.objects.get(pk=order.id).status == "CANCELLED"
    assert Wallet.objects.get(user=wallet_actor).available_units == 10000000000
    with pytest.raises(DomainError):
        buy(wallet_actor, instrument)
    assert TradingSelector.orders(wallet_actor).count() == 0
    assert Reconciliation.run() == ()


def test_fill_rechecks_current_authorization(wallet_actor, instrument, publish):
    fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    publish()
    wallet_actor.groups.clear()
    assert execute(order) is None
    assert not Fill.objects.exists()
    assert Order.objects.get(pk=order.id).status == "CANCELLED"
    assert Reconciliation.run() == ()


def test_cross_owner_orders_are_hidden(wallet_actor, instrument, django_user_model):
    fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    other = django_user_model.objects.create_user(username="other-trader")
    other.groups.set(wallet_actor.groups.all())
    with pytest.raises(DomainError) as error:
        CancelOrder().execute(other, CancelOrderCommand(order.id), "steal")
    assert error.value.status == 404
    assert not TradingSelector.orders(other).exists()
    assert not TradingSelector.fills(other).exists()
    assert TradingSelector.portfolio(other) == ()


@pytest.mark.parametrize("fields", [
    {"budget": "-1"}, {"budget": "0"}, {"budget": "0.000000001"},
    {"budget": "100", "quantity": "1.000"}, {"budget": "0.01"},
])
def test_invalid_order_input_leaves_no_reservations(wallet_actor, instrument, fields):
    fund(wallet_actor)
    with pytest.raises(DomainError):
        PlaceOrder().execute(wallet_actor, PlaceOrderCommand(str(instrument.pk), "BUY", "MARKET", **fields), "invalid")
    assert not Order.objects.exists()
    assert Wallet.objects.get(user=wallet_actor).reserved_units == 0


def test_fill_failure_rolls_back_all_effects(wallet_actor, instrument, publish, monkeypatch):
    fund(wallet_actor)
    order = buy(wallet_actor, instrument)
    publish()
    before = Journal.objects.count()

    def failure(*args, **kwargs):
        raise RuntimeError("lot failure")
    monkeypatch.setattr("apps.trading.services.positions.PositionBook.buy", failure)
    with pytest.raises(RuntimeError):
        execute(order)
    assert not Fill.objects.exists()
    assert not AssetMovement.objects.exists()
    assert Journal.objects.count() == before
    assert Order.objects.get(pk=order.id).status == "OPEN"
    assert Reconciliation.run() == ()


def test_eod_requires_completed_later_cairo_session(wallet_actor, instrument, publish, clock):
    instrument.market = "EGX"
    instrument.currency = "EGP"
    instrument.data_mode = "EOD"
    instrument.delay_seconds = 86400
    instrument.cadence_seconds = 86400
    instrument.minimum_notional = 1
    instrument.save()
    quote = Quote.objects.get(instrument=instrument)
    PriceObservation.objects.create(instrument=instrument, kind="BAR", price_units=quote.price_units,
        source_at=quote.source_at, received_at=quote.received_at,
        observation_id=quote.observation_id, generation=quote.generation, tradable=True)
    fund(wallet_actor, currency="EGP")
    order = buy(wallet_actor, instrument)
    assert Order.objects.get(pk=order.id).expires_at > clock["now"] + timedelta(days=6)
    publish(9000)
    assert execute(order) is None  # Same Cairo session, even with a later clock timestamp.
    publish(9000, seconds=86400)
    assert execute(order) is not None
    assert Reconciliation.run() == ()
