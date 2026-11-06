import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.common.exceptions import DomainError

from .policies import MAX_COOLDOWN_SECONDS, AlertConfigurationPolicy, default_cooldown


class AlertRule(models.Model):
    class Kind(models.TextChoices):
        PRICE_ABOVE = "PRICE_ABOVE", _("Price above")
        PRICE_BELOW = "PRICE_BELOW", _("Price below")
        PERCENT_RISE = "PERCENT_RISE", _("Percentage rise")
        PERCENT_DROP = "PERCENT_DROP", _("Percentage drop")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alert_rules")
    instrument = models.ForeignKey("markets.Instrument", on_delete=models.PROTECT, related_name="alert_rules")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Validated canonical text avoids SQLite's REAL-backed decimal arithmetic.
    threshold = models.CharField(max_length=32)
    window_seconds = models.PositiveIntegerField(null=True, blank=True)
    cooldown_seconds = models.PositiveIntegerField(default=default_cooldown)
    enabled = models.BooleanField(default=True)
    last_condition = models.BooleanField(null=True, default=None)
    last_observation_id = models.CharField(max_length=128, blank=True, default="")
    generation = models.UUIDField(null=True, default=None)
    last_source_at = models.DateTimeField(null=True, default=None)
    last_triggered_at = models.DateTimeField(null=True, default=None)
    baseline_at = models.DateTimeField(null=True, default=None)
    state_reason = models.CharField(max_length=40, default="awaiting_observation")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="alert_owner_created"),
            models.Index(fields=["enabled", "instrument"], name="alert_enabled_instrument"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(kind__in=["PRICE_ABOVE", "PRICE_BELOW"], window_seconds__isnull=True)
                           | Q(kind__in=["PERCENT_RISE", "PERCENT_DROP"], window_seconds__gt=0,
                               window_seconds__isnull=False)),
                name="alert_kind_window_valid",
            ),
            models.CheckConstraint(condition=Q(cooldown_seconds__lte=MAX_COOLDOWN_SECONDS), name="alert_cooldown_bounded"),
        ]

    def clean(self):
        super().clean()
        try:
            self.threshold = AlertConfigurationPolicy.validate(
                instrument=self.instrument, kind=self.kind, threshold=self.threshold,
                window_seconds=self.window_seconds, cooldown_seconds=self.cooldown_seconds,
                enabled=self.enabled,
            )
        except DomainError as exc:
            raise ValidationError(exc.fields or {"threshold": exc.message}) from exc

    def save(self, *args, **kwargs):
        # Evaluation-only saves must remain possible after instrument cadence changes.
        fields = kwargs.get("update_fields")
        configuration = {"instrument", "instrument_id", "kind", "threshold", "window_seconds",
                         "cooldown_seconds", "enabled"}
        if fields is None or configuration.intersection(fields):
            self.clean()
        return super().save(*args, **kwargs)
