"""Normalized market storage. No provider credentials or vendor payloads are persisted."""

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

MAX_UNITS = 2**63 - 1


class Instrument(models.Model):
    class Market(models.TextChoices):
        CRYPTO = "CRYPTO", "Crypto"
        EGX = "EGX", "EGX"

    class DataMode(models.TextChoices):
        LIVE = "LIVE", "Live"
        DELAYED = "DELAYED", "Delayed"
        EOD = "EOD", "End of day"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    symbol = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    market = models.CharField(max_length=8, choices=Market.choices)
    currency = models.CharField(max_length=4, choices=[("USDT", "USDT"), ("EGP", "EGP")])
    provider = models.CharField(max_length=24)
    provider_symbol = models.CharField(max_length=64)
    price_scale = models.PositiveSmallIntegerField(validators=[MaxValueValidator(18)])
    quantity_scale = models.PositiveSmallIntegerField(validators=[MaxValueValidator(18)])
    quantity_step = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    minimum_quantity = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    minimum_notional = models.PositiveBigIntegerField(default=0)
    enabled = models.BooleanField(default=True)
    data_mode = models.CharField(max_length=8, choices=DataMode.choices, default=DataMode.LIVE)
    delay_seconds = models.PositiveIntegerField(default=0)
    cadence_seconds = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1)])
    price_step = models.PositiveBigIntegerField(default=1, validators=[MinValueValidator(1)])
    venue_timezone = models.CharField(max_length=40, default="UTC")
    watched = models.BooleanField(default=True)
    halted_reason = models.CharField(max_length=64, blank=True)
    metadata_fingerprint = models.CharField(max_length=64, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["market", "symbol", "id"]
        constraints = [
            models.UniqueConstraint(fields=["market", "symbol"], name="market_symbol_unique"),
            models.UniqueConstraint(fields=["provider", "provider_symbol"], name="market_mapping_unique"),
            models.CheckConstraint(condition=Q(price_scale__lte=18, quantity_scale__lte=18), name="market_scales_bounded"),
            models.CheckConstraint(condition=Q(quantity_step__gt=0, minimum_quantity__gt=0, price_step__gt=0, cadence_seconds__gt=0), name="market_rules_positive"),
            models.CheckConstraint(condition=Q(market="CRYPTO", currency="USDT") | Q(market="EGX", currency="EGP"), name="market_currency_supported"),
        ]


class ObservationValues(models.Model):
    price_units = models.PositiveBigIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_UNITS)])
    source_at = models.DateTimeField(db_index=True)
    received_at = models.DateTimeField()
    observation_id = models.CharField(max_length=128)
    generation = models.UUIDField(default=uuid.uuid4)
    tradable = models.BooleanField(default=False)

    class Meta:
        abstract = True


class Quote(ObservationValues):
    instrument = models.OneToOneField(Instrument, on_delete=models.CASCADE, related_name="quote", primary_key=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(price_units__gt=0, price_units__lte=MAX_UNITS), name="quote_price_positive")]


class PriceObservation(ObservationValues):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="observations")
    kind = models.CharField(max_length=8, choices=[("TRADE", "Trade"), ("BAR", "Completed bar")], default="TRADE")

    class Meta:
        ordering = ["-source_at", "-id"]
        indexes = [models.Index(fields=["instrument", "-source_at"], name="market_history_time")]
        constraints = [
            models.UniqueConstraint(fields=["instrument", "generation", "observation_id"], name="market_observation_unique"),
            models.CheckConstraint(condition=Q(price_units__gt=0, price_units__lte=MAX_UNITS), name="observation_price_positive"),
        ]


class CompletedCandle(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="candles")
    source_id = models.CharField(max_length=128)
    generation = models.UUIDField()
    interval = models.CharField(max_length=8)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(db_index=True)
    open_units = models.PositiveBigIntegerField()
    high_units = models.PositiveBigIntegerField()
    low_units = models.PositiveBigIntegerField()
    close_units = models.PositiveBigIntegerField()
    volume_units = models.PositiveBigIntegerField()

    class Meta:
        ordering = ["-closed_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["instrument", "generation", "source_id"], name="market_candle_unique"),
            models.CheckConstraint(condition=Q(low_units__gt=0) & Q(low_units__lte=models.F("open_units")) & Q(low_units__lte=models.F("close_units")) & Q(high_units__gte=models.F("open_units")) & Q(high_units__gte=models.F("close_units")), name="market_candle_prices_valid"),
        ]


class ProviderHealth(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"
        DEGRADED = "DEGRADED", "Degraded"
        BACKOFF = "BACKOFF", "Backoff"

    provider = models.CharField(max_length=24, primary_key=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UNAVAILABLE)
    reason = models.CharField(max_length=64, default="NOT_STARTED")
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    failures = models.PositiveIntegerField(default=0)
    generation = models.UUIDField(default=uuid.uuid4)
    metadata_at = models.DateTimeField(null=True, blank=True)


class ProviderUsage(models.Model):
    """Reservations are persisted before HTTP, including calls that fail upstream."""

    provider = models.CharField(max_length=24)
    window_start = models.DateTimeField()
    used = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "window_start"], name="market_usage_window_unique")]


class Watchlist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="market_watchlist")
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="watchlists")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["user", "instrument"], name="market_watchlist_unique")]
