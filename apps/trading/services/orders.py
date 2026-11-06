from dataclasses import asdict

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.common.idempotency import Idempotency
from apps.events.services import EventPublisher
from apps.identity.policies import AuthorizationPolicy
from apps.markets.models import Instrument, Quote
from apps.trading.dtos import CancelOrderCommand, MovementCommand, OrderDTO, PlaceOrderCommand
from apps.trading.models import Order
from apps.trading.policies.execution import ExecutionRules, QuoteEligibility
from apps.trading.services.positions import PositionBook
from apps.wallets.dtos import PostingCommand, PostingLine
from apps.wallets.models import financial_write
from apps.wallets.money import Money, decimal_string, to_units
from apps.wallets.services.ledger import LedgerPoster


def order_dto(order) -> OrderDTO:
    return OrderDTO(
        str(order.pk), str(order.instrument_id), order.instrument.symbol, order.currency,
        order.side, order.order_type,
        decimal_string(order.quantity_units, order.quantity_scale) if order.quantity_units is not None else None,
        decimal_string(order.limit_price_units, order.price_scale) if order.limit_price_units is not None else None,
        Money(order.currency, order.budget_units).amount if order.budget_units is not None else None,
        Money(order.currency, order.reserved_cash_units).amount,
        decimal_string(order.reserved_quantity_units, order.quantity_scale), order.status,
        order.created_at.isoformat(), order.accepted_at.isoformat(),
        order.expires_at.isoformat() if order.expires_at else None,
        order.closed_at.isoformat() if order.closed_at else None, order.close_reason,
        order.source_watermark, order.generation,
    )


def require_order(actor, order, action):
    # Ownership remains non-overridable even if policies expand in future.
    if actor.pk != order.user_id:
        raise DomainError("not_found", _("Order not found."), status=404)
    AuthorizationPolicy.require(actor, action, owner_id=order.user_id,
                                 market=order.instrument.market, currency=order.currency)


def load_order(actor, order_id):
    order = Order.objects.select_related("user", "instrument").filter(pk=order_id, user=actor).first()
    if order is None:
        raise DomainError("not_found", _("Order not found."), status=404)
    return order


def emit_order(actor, order, action, request_id=""):
    payload = asdict(order_dto(order))
    AuditRecorder.record(actor, action, "order", str(order.pk), changes=payload,
                         request_id=request_id, reason=order.close_reason)
    EventPublisher.publish(order.user, "order.changed", payload)


class PlaceOrder:
    def execute(self, actor, command: PlaceOrderCommand, key: str) -> OrderDTO:
        with transaction.atomic():
            instrument = Instrument.objects.filter(pk=command.instrument_id).first()
            if instrument is None:
                raise DomainError("not_found", _("Instrument not found."), status=404)

            def authorize():
                AuthorizationPolicy.require(actor, "trading.place", owner_id=actor.pk,
                                             market=instrument.market, currency=instrument.currency)

            authorize()

            def action():
                authorize()
                now = timezone.now()
                quote = Quote.objects.filter(instrument=instrument).first()
                QuoteEligibility.require_acceptance(instrument, quote, now)
                if quote is None:
                    raise DomainError("market_data_unavailable", _("Market data is unavailable."))
                if command.side not in Order.Side.values or command.order_type not in Order.Type.values:
                    raise DomainError("invalid_order", _("Unsupported order side or type."))
                price_units = quote.price_units
                market_buy = command.side == "BUY" and command.order_type == "MARKET"
                cash = 0
                if market_buy:
                    if command.budget is None or command.quantity is not None or command.limit_price is not None:
                        raise DomainError("invalid_order", _("Market buys require only a cash budget."))
                    budget = Money.from_decimal(instrument.currency, command.budget).units
                    quantity = None
                    limit = None
                    qty = Money.affordable_quantity(
                        instrument.currency, budget, price_units, instrument.price_scale,
                        instrument.quantity_scale, instrument.quantity_step)
                    ExecutionRules.validate_quantity(instrument, qty, price_units)
                    cash = budget
                else:
                    if command.quantity is None or command.budget is not None or (command.limit_price is not None) != (command.order_type == "LIMIT"):
                        raise DomainError("invalid_order", _("Provide quantity, and a price only for limit orders."))
                    qty = to_units(command.quantity, instrument.quantity_scale)
                    quantity = qty
                    limit = to_units(command.limit_price, instrument.price_scale) if command.limit_price is not None else None
                    budget = None
                    ExecutionRules.validate_quantity(instrument, qty, limit or price_units)
                    if command.side == "BUY":
                        if limit is None:
                            raise DomainError("invalid_order", _("Provide a limit price for limit orders."))
                        cash = ExecutionRules.cash(instrument, limit, qty, buy=True)
                    else:
                        ExecutionRules.cash(instrument, limit or price_units, qty, buy=False)
                with financial_write():
                    order = Order.objects.create(
                        user=actor, instrument=instrument, side=command.side, order_type=command.order_type,
                        quantity_units=quantity, limit_price_units=limit, budget_units=budget,
                        reserved_cash_units=cash, reserved_quantity_units=qty if command.side == "SELL" else 0,
                        currency=instrument.currency, price_scale=instrument.price_scale,
                        quantity_scale=instrument.quantity_scale, provider=instrument.provider,
                        data_mode=instrument.data_mode, source_at=quote.source_at,
                        source_watermark=quote.observation_id, generation=str(quote.generation),
                        last_source_at=quote.source_at, last_watermark=quote.observation_id,
                        accepted_at=now, created_at=now,
                        expires_at=QuoteEligibility.deadline(instrument, now) if command.order_type == "MARKET" else None,
                    )
                if command.side == "BUY":
                    # Reject precision changes even when a previously held position is flat.
                    PositionBook.get(actor, instrument)
                    LedgerPoster.post(actor, LedgerPoster.wallet(actor, instrument.currency), PostingCommand(
                        "RESERVE", (PostingLine("AVAILABLE", -cash), PostingLine("RESERVED", cash)),
                        reference=str(order.pk), request_id=command.request_id))
                else:
                    position = PositionBook.get(actor, instrument)
                    PositionBook.move(actor, position, order, MovementCommand("RESERVE", -qty, qty), command.request_id)
                emit_order(actor, order, "trading.place", command.request_id)
                return asdict(order_dto(order))

            payload = asdict(command)
            payload.pop("request_id")
            result = Idempotency.execute(actor, "trading.place", key, payload, action)
            return OrderDTO(**result)


class CloseOrder:
    """Internal atomic state transition used by cancellation, expiry and access revocation."""

    @staticmethod
    def apply(actor, order, status, reason, request_id="", now=None):
        if status not in (Order.Status.CANCELLED, Order.Status.EXPIRED):
            raise DomainError("invalid_transition", _("Invalid closing transition."), status=409)
        if order.status != Order.Status.OPEN:
            raise DomainError("terminal_order", _("The order is already closed."), status=409)
        # Cash uses acceptance currency even when catalog metadata has been suspended/changed.
        if order.reserved_cash_units:
            cash = order.reserved_cash_units
            LedgerPoster.post(actor, LedgerPoster.wallet(order.user, order.currency), PostingCommand(
                "RELEASE", (PostingLine("AVAILABLE", cash), PostingLine("RESERVED", -cash)),
                reference=str(order.pk), reason=reason, request_id=request_id))
        if order.reserved_quantity_units:
            from apps.trading.models import Position
            position = Position.objects.get(user=order.user, instrument=order.instrument)
            quantity = order.reserved_quantity_units
            PositionBook.move(actor, position, order, MovementCommand("RELEASE", quantity, -quantity), request_id)
        with financial_write():
            order.status = status
            order.closed_at = now or timezone.now()
            order.close_reason = reason
            order.reserved_cash_units = 0
            order.reserved_quantity_units = 0
            order.save(update_fields=["status", "closed_at", "close_reason", "reserved_cash_units", "reserved_quantity_units"])
        emit_order(actor, order, f"trading.{status.lower()}", request_id)
        return order_dto(order)


class CancelOrder:
    def execute(self, actor, command: CancelOrderCommand, key: str) -> OrderDTO:
        with transaction.atomic():
            order = load_order(actor, command.order_id)
            require_order(actor, order, "trading.cancel")

            def action():
                require_order(actor, order, "trading.cancel")
                return asdict(CloseOrder.apply(actor, order, Order.Status.CANCELLED, "user_cancelled", command.request_id))

            return OrderDTO(**Idempotency.execute(actor, "trading.cancel", key,
                              {"order_id": command.order_id}, action))
