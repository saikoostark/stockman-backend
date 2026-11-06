from dataclasses import asdict

from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.events.services import EventPublisher
from apps.identity.policies import AuthorizationPolicy
from apps.markets.models import Quote
from apps.trading.dtos import ExecuteOrderCommand, FillDTO, MovementCommand
from apps.trading.models import Fill, Order
from apps.trading.policies.execution import ExecutionRules, QuoteEligibility
from apps.trading.services.orders import CloseOrder, emit_order
from apps.trading.services.positions import PositionBook
from apps.wallets.dtos import PostingCommand, PostingLine
from apps.wallets.models import financial_write
from apps.wallets.money import Money, checked_units, decimal_string
from apps.wallets.services.ledger import LedgerPoster


def fill_dto(fill) -> FillDTO:
    order = fill.order
    return FillDTO(str(fill.pk), str(order.pk), str(order.instrument_id), order.instrument.symbol,
        order.currency, order.side, decimal_string(fill.quantity_units, order.quantity_scale),
        decimal_string(fill.price_units, order.price_scale), Money(order.currency, fill.cash_units).amount,
        Money(order.currency, fill.cost_basis_units).amount, Money(order.currency, fill.realized_pnl_units).amount,
        Money(order.currency, 0).amount, fill.source_at.isoformat(), fill.source_watermark,
        fill.generation, fill.created_at.isoformat())


def placement_allowed(order):
    return AuthorizationPolicy.allows(order.user, "trading.place", owner_id=order.user_id,
                                       market=order.instrument.market, currency=order.currency)


class ExecuteOrder:
    """Worker command. State + unique Fill.order provide transactional idempotency."""

    def execute(self, command: ExecuteOrderCommand, *, now=None) -> FillDTO | None:
        with transaction.atomic():
            now = now or timezone.now()
            order = Order.objects.select_related("user", "instrument").filter(pk=command.order_id).first()
            if order is None:
                raise DomainError("not_found", _("Order not found."), status=404)
            if not placement_allowed(order):
                if order.status == Order.Status.OPEN:
                    CloseOrder.apply(None, order, Order.Status.CANCELLED, "authorization_revoked", command.request_id, now)
                return None
            if order.status == Order.Status.FILLED:
                return fill_dto(Fill.objects.get(order=order))
            if order.status != Order.Status.OPEN:
                return None
            if order.expires_at is not None and order.expires_at <= now:
                CloseOrder.apply(None, order, Order.Status.EXPIRED, "timeout", command.request_id, now)
                return None
            instrument = order.instrument
            quote = Quote.objects.filter(instrument=instrument).first()
            if not QuoteEligibility.eligible(order, quote, now):
                return None
            if quote is None:
                return None
            with financial_write():
                order.last_source_at = quote.source_at
                order.last_watermark = quote.observation_id
                order.save(update_fields=["last_source_at", "last_watermark"])
            if not ExecutionRules.crosses(order, quote.price_units):
                return None
            quantity = order.quantity_units
            if order.side == "BUY" and order.order_type == "MARKET":
                quantity = Money.affordable_quantity(order.currency, order.budget_units, quote.price_units,
                    order.price_scale, order.quantity_scale, instrument.quantity_step)
            try:
                ExecutionRules.validate_quantity(instrument, quantity, quote.price_units)
                cash = ExecutionRules.cash(instrument, quote.price_units, quantity, buy=order.side == "BUY")
            except DomainError as exc:
                if exc.code not in {"invalid_quantity", "minimum_notional", "invalid_amount"}:
                    raise
                # No supported complete fill at this observation; wait for another or timeout.
                return None
            if order.side == "BUY" and cash > order.reserved_cash_units:
                raise DomainError("reservation_discrepancy", _("Order cost exceeds its cash reservation."), status=409)
            position = PositionBook.get(order.user, instrument)
            allocations = PositionBook.allocations(position, quantity) if order.side == "SELL" else ()
            cost_basis = checked_units(sum(a.cost_units for a in allocations)) if allocations else cash
            realized = checked_units(cash - cost_basis) if order.side == "SELL" else 0
            lines: tuple[PostingLine, ...]
            if order.side == "BUY":
                lines = (PostingLine("RESERVED", -order.reserved_cash_units),
                         PostingLine("AVAILABLE", order.reserved_cash_units - cash),
                         PostingLine("CLEARING", cash))
            else:
                if order.reserved_quantity_units != quantity:
                    raise DomainError("reservation_discrepancy", _("Order quantity differs from its asset reservation."), status=409)
                lines = (PostingLine("AVAILABLE", cash), PostingLine("CLEARING", -cash))
            journal = LedgerPoster.post(None, LedgerPoster.wallet(order.user, order.currency), PostingCommand(
                "SETTLEMENT", lines, reference=str(order.pk), request_id=command.request_id))
            with financial_write():
                fill = Fill.objects.create(order=order, quantity_units=quantity, price_units=quote.price_units,
                    cash_units=cash, cost_basis_units=cost_basis, realized_pnl_units=realized,
                    source_at=quote.source_at, source_watermark=quote.observation_id,
                    generation=str(quote.generation), journal=journal, created_at=now)
            if order.side == "BUY":
                PositionBook.move(None, position, order, MovementCommand("BUY", quantity, 0, -quantity), command.request_id)
                PositionBook.buy(position, fill)
            else:
                PositionBook.move(None, position, order, MovementCommand("SELL", 0, -quantity, quantity), command.request_id)
                PositionBook.sell(position, fill, allocations)
            with financial_write():
                order.status = Order.Status.FILLED
                order.closed_at = now
                order.reserved_cash_units = 0
                order.reserved_quantity_units = 0
                order.save(update_fields=["status", "closed_at", "reserved_cash_units", "reserved_quantity_units"])
            dto = fill_dto(fill)
            AuditRecorder.record(None, "trading.fill", "fill", str(fill.pk), changes=asdict(dto), request_id=command.request_id)
            EventPublisher.publish(order.user, "order.filled", asdict(dto))
            emit_order(None, order, "trading.filled", command.request_id)
            return dto


class OrderMatcher:
    def run(self, *, now=None, batch_size=200) -> int:
        # No provider calls. Each order takes its own short write transaction.
        ids = list(Order.objects.filter(status=Order.Status.OPEN).order_by("accepted_at", "id")
                   .values_list("pk", flat=True)[:max(1, min(batch_size, 1000))])
        matched = 0
        for order_id in ids:
            result = ExecuteOrder().execute(ExecuteOrderCommand(str(order_id)), now=now)
            matched += result is not None
        return matched


class AccessRevocation:
    @staticmethod
    def cancel_unauthorized(user_ids=None) -> int:
        """Identity calls this INSIDE its transaction; no revoked cancel grant is required."""
        if not connection.in_atomic_block:
            raise RuntimeError("Access revocation requires the existing identity transaction")
        orders = Order.objects.filter(status=Order.Status.OPEN).select_related("user", "instrument")
        if user_ids is not None:
            orders = orders.filter(user_id__in=user_ids)
        count = 0
        for order in orders.order_by("id"):
            if not placement_allowed(order):
                CloseOrder.apply(None, order, Order.Status.CANCELLED, "authorization_revoked")
                count += 1
        return count
