import uuid
from typing import Any, ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.wallets.models import AppendOnlyModel, FinancialModel, FinancialQuerySet


class OrderQuerySet(FinancialQuerySet):
    def update(self, **kwargs):
        raise DomainError("invalid_transition", _("Use the order state machine."), status=409)

    def bulk_create(self, *args, **kwargs):
        raise DomainError("invalid_transition", _("Use the order state machine."), status=409)


class Order(FinancialModel):
    class Side(models.TextChoices):
        BUY = "BUY"
        SELL = "SELL"

    class Type(models.TextChoices):
        MARKET = "MARKET"
        LIMIT = "LIMIT"

    class Status(models.TextChoices):
        OPEN = "OPEN"
        FILLED = "FILLED"
        CANCELLED = "CANCELLED"
        EXPIRED = "EXPIRED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    instrument = models.ForeignKey("markets.Instrument", on_delete=models.PROTECT, related_name="orders")
    side = models.CharField(max_length=4, choices=Side.choices)
    order_type = models.CharField(max_length=6, choices=Type.choices)
    quantity_units = models.BigIntegerField(null=True, blank=True)
    limit_price_units = models.BigIntegerField(null=True, blank=True)
    budget_units = models.BigIntegerField(null=True, blank=True)
    reserved_cash_units = models.BigIntegerField(default=0)
    reserved_quantity_units = models.BigIntegerField(default=0)
    # Snapshot precision prevents later catalog changes silently reinterpreting holdings/orders.
    currency = models.CharField(max_length=4)
    price_scale = models.PositiveSmallIntegerField()
    quantity_scale = models.PositiveSmallIntegerField()
    provider = models.CharField(max_length=64)
    data_mode = models.CharField(max_length=8)
    source_at = models.DateTimeField()
    source_watermark = models.CharField(max_length=255)
    generation = models.CharField(max_length=128)
    last_source_at = models.DateTimeField()
    last_watermark = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    accepted_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=64, blank=True)
    objects: ClassVar[models.Manager[Any]] = OrderQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "expires_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(side__in=["BUY", "SELL"]), name="order_side_valid"),
            models.CheckConstraint(condition=Q(status__in=["OPEN", "FILLED", "CANCELLED", "EXPIRED"]), name="order_status_valid"),
            models.CheckConstraint(condition=Q(reserved_cash_units__gte=0, reserved_quantity_units__gte=0), name="order_reserves_nonnegative"),
            models.CheckConstraint(condition=Q(quantity_units__isnull=True) | Q(quantity_units__gt=0), name="order_quantity_positive"),
            models.CheckConstraint(condition=Q(limit_price_units__isnull=True) | Q(limit_price_units__gt=0), name="order_price_positive"),
            models.CheckConstraint(condition=Q(budget_units__isnull=True) | Q(budget_units__gt=0), name="order_budget_positive"),
            models.CheckConstraint(condition=(
                Q(order_type="MARKET", side="BUY", budget_units__isnull=False, quantity_units__isnull=True, limit_price_units__isnull=True)
                | Q(order_type="MARKET", side="SELL", budget_units__isnull=True, quantity_units__isnull=False, limit_price_units__isnull=True)
                | Q(order_type="LIMIT", budget_units__isnull=True, quantity_units__isnull=False, limit_price_units__isnull=False)
            ), name="order_shape_valid"),
            models.CheckConstraint(condition=(Q(status="OPEN", closed_at__isnull=True)
                | (~Q(status="OPEN") & Q(closed_at__isnull=False, reserved_cash_units=0, reserved_quantity_units=0))), name="order_terminal_released"),
            models.CheckConstraint(condition=Q(order_type="LIMIT", expires_at__isnull=True) | Q(order_type="MARKET", expires_at__isnull=False), name="order_timeout_required"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if old.status != self.Status.OPEN:
                raise DomainError("terminal_order", _("Terminal orders cannot change."), status=409)
            mutable = {"status", "closed_at", "close_reason", "reserved_cash_units", "reserved_quantity_units", "last_source_at", "last_watermark"}
            for field in self._meta.concrete_fields:
                if field.name not in mutable and getattr(old, field.attname) != getattr(self, field.attname):
                    raise DomainError("immutable_order", _("Accepted order terms cannot change."), status=409)
        return super().save(*args, **kwargs)


class Fill(AppendOnlyModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="fill")
    quantity_units = models.BigIntegerField()
    price_units = models.BigIntegerField()
    cash_units = models.BigIntegerField()
    cost_basis_units = models.BigIntegerField(default=0)
    realized_pnl_units = models.BigIntegerField(default=0)
    source_at = models.DateTimeField()
    source_watermark = models.CharField(max_length=255)
    generation = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    journal = models.OneToOneField("wallets.Journal", on_delete=models.PROTECT, related_name="fill")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(quantity_units__gt=0, price_units__gt=0, cash_units__gt=0, cost_basis_units__gte=0), name="fill_values_positive")]


class Position(FinancialModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="positions")
    instrument = models.ForeignKey("markets.Instrument", on_delete=models.PROTECT, related_name="positions")
    currency = models.CharField(max_length=4)
    quantity_scale = models.PositiveSmallIntegerField()
    available_units = models.BigIntegerField(default=0)
    reserved_units = models.BigIntegerField(default=0)
    cost_basis_units = models.BigIntegerField(default=0)
    realized_pnl_units = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "instrument"], name="position_user_instrument"),
            models.CheckConstraint(condition=Q(available_units__gte=0, reserved_units__gte=0, cost_basis_units__gte=0), name="position_nonnegative"),
        ]


class AssetMovement(AppendOnlyModel):
    id = models.BigAutoField(primary_key=True)
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="movements")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="asset_movements")
    kind = models.CharField(max_length=12, choices=[(v, v) for v in ("RESERVE", "RELEASE", "BUY", "SELL")])
    available_delta = models.BigIntegerField()
    reserved_delta = models.BigIntegerField()
    counterparty_delta = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "kind"], name="asset_order_kind_unique"),
            models.CheckConstraint(condition=Q(available_delta=-models.F("reserved_delta") - models.F("counterparty_delta")), name="asset_movement_balanced"),
        ]


class CostLot(FinancialModel):
    id = models.BigAutoField(primary_key=True)
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="lots")
    fill = models.OneToOneField(Fill, on_delete=models.PROTECT, related_name="lot")
    original_quantity_units = models.BigIntegerField()
    remaining_quantity_units = models.BigIntegerField()
    original_cost_units = models.BigIntegerField()
    remaining_cost_units = models.BigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.CheckConstraint(condition=Q(
            original_quantity_units__gt=0, original_cost_units__gt=0,
            remaining_quantity_units__gte=0, remaining_cost_units__gte=0,
            remaining_quantity_units__lte=models.F("original_quantity_units"),
            remaining_cost_units__lte=models.F("original_cost_units")), name="lot_values_valid")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            for field in self._meta.concrete_fields:
                if field.name not in {"remaining_quantity_units", "remaining_cost_units"} and getattr(old, field.attname) != getattr(self, field.attname):
                    raise DomainError("immutable_lot", _("Original lot values cannot change."), status=409)
            if self.remaining_quantity_units > old.remaining_quantity_units or self.remaining_cost_units > old.remaining_cost_units:
                raise DomainError("immutable_lot", _("Consumed lots cannot be restored."), status=409)
        return super().save(*args, **kwargs)


class LotConsumption(AppendOnlyModel):
    id = models.BigAutoField(primary_key=True)
    lot = models.ForeignKey(CostLot, on_delete=models.PROTECT, related_name="consumptions")
    fill = models.ForeignKey(Fill, on_delete=models.PROTECT, related_name="consumptions")
    quantity_units = models.BigIntegerField()
    cost_units = models.BigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lot", "fill"], name="lot_fill_consumption_unique"),
            models.CheckConstraint(condition=Q(quantity_units__gt=0, cost_units__gte=0), name="lot_consumption_positive"),
        ]
