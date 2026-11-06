from dataclasses import dataclass

from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.events.services import EventPublisher
from apps.trading.dtos import MovementCommand
from apps.trading.models import AssetMovement, CostLot, LotConsumption, Position
from apps.wallets.models import financial_write
from apps.wallets.money import checked_units, decimal_string


@dataclass(frozen=True, slots=True)
class LotAllocation:
    lot_id: int
    quantity_units: int
    cost_units: int


class PositionBook:
    @staticmethod
    def get(user, instrument):
        with financial_write():
            position, _created = Position.objects.get_or_create(
                user=user, instrument=instrument,
                defaults={"currency": instrument.currency, "quantity_scale": instrument.quantity_scale},
            )
        if position.currency != instrument.currency or position.quantity_scale != instrument.quantity_scale:
            raise DomainError("instrument_changed", _("Instrument precision changed; trading is suspended."), status=409)
        return position

    @staticmethod
    def move(actor, position, order, command: MovementCommand, request_id=""):
        available = checked_units(position.available_units + command.available_delta)
        reserved = checked_units(position.reserved_units + command.reserved_delta)
        checked_units(available + reserved)
        if available < 0 or reserved < 0:
            raise DomainError("insufficient_holdings", _("Insufficient available owned quantity."), status=409)
        if command.available_delta + command.reserved_delta + command.counterparty_delta:
            raise DomainError("unbalanced_assets", _("Asset movements must balance."))
        with financial_write():
            movement = AssetMovement.objects.create(
                position=position, order=order, kind=command.kind,
                available_delta=checked_units(command.available_delta),
                reserved_delta=checked_units(command.reserved_delta),
                counterparty_delta=checked_units(command.counterparty_delta),
            )
            position.available_units = available
            position.reserved_units = reserved
            position.save(update_fields=["available_units", "reserved_units"])
        payload = {"instrument_id": str(position.instrument_id), "order_id": str(order.pk),
                   "available": decimal_string(available, position.quantity_scale),
                   "reserved": decimal_string(reserved, position.quantity_scale)}
        AuditRecorder.record(actor, f"trading.assets.{command.kind.lower()}", "asset_movement", str(movement.pk), changes=payload, request_id=request_id)
        EventPublisher.publish(position.user, "position.changed", payload)

    @staticmethod
    def allocations(position, quantity: int) -> tuple[LotAllocation, ...]:
        remaining = quantity
        allocations = []
        for lot in position.lots.filter(remaining_quantity_units__gt=0).order_by("created_at", "id"):
            take = min(remaining, lot.remaining_quantity_units)
            consumed_quantity = lot.original_quantity_units - lot.remaining_quantity_units
            consumed_cost = lot.original_cost_units - lot.remaining_cost_units
            # Cumulative floor allocation makes partial FIFO consumption partition-independent.
            cost = lot.original_cost_units * (consumed_quantity + take) // lot.original_quantity_units - consumed_cost
            allocations.append(LotAllocation(lot.pk, take, cost))
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise DomainError("holdings_discrepancy", _("Owned quantity does not reconcile to FIFO lots."), status=409)
        return tuple(allocations)

    @staticmethod
    def buy(position, fill):
        with financial_write():
            CostLot.objects.create(position=position, fill=fill,
                original_quantity_units=fill.quantity_units, remaining_quantity_units=fill.quantity_units,
                original_cost_units=fill.cash_units, remaining_cost_units=fill.cash_units)
            position.cost_basis_units = checked_units(position.cost_basis_units + fill.cash_units)
            position.save(update_fields=["cost_basis_units"])

    @staticmethod
    def sell(position, fill, allocations: tuple[LotAllocation, ...]):
        with financial_write():
            for allocation in allocations:
                lot = CostLot.objects.get(pk=allocation.lot_id)
                lot.remaining_quantity_units -= allocation.quantity_units
                lot.remaining_cost_units -= allocation.cost_units
                lot.save(update_fields=["remaining_quantity_units", "remaining_cost_units"])
                LotConsumption.objects.create(lot=lot, fill=fill, quantity_units=allocation.quantity_units, cost_units=allocation.cost_units)
            position.cost_basis_units = checked_units(position.cost_basis_units - fill.cost_basis_units)
            position.realized_pnl_units = checked_units(position.realized_pnl_units + fill.realized_pnl_units)
            position.save(update_fields=["cost_basis_units", "realized_pnl_units"])
