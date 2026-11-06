import hashlib
from datetime import timedelta
from decimal import Decimal, localcontext

from django.db import transaction
from django.utils import timezone

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.events.services import NotificationPublisher
from apps.identity.policies import AuthorizationPolicy
from apps.markets.models import Quote

from .models import AlertRule
from .policies import (
    PRICE_KINDS,
    AlertConfigurationPolicy,
    AlertObservationPolicy,
    canonical_decimal,
)
from .selectors import AlertHistorySelector

STATE_FIELDS = [
    "last_condition", "last_observation_id", "generation", "last_source_at",
    "last_triggered_at", "baseline_at", "state_reason", "updated_at",
]


class EvaluateAlerts:
    """Consume advancing observations, not historical crossings or timer-based reminders."""

    @classmethod
    def run(cls, *, clock=None) -> int:
        clock = clock or timezone.now
        published = 0
        ids = AlertRule.objects.filter(enabled=True).values_list("pk", flat=True)
        for rule_id in ids.iterator(chunk_size=200):
            # SQLite IMMEDIATE (configured globally) serializes evaluation with API mutations.
            with transaction.atomic():
                rule = AlertRule.objects.filter(pk=rule_id, enabled=True).select_related(
                    "user", "instrument",
                ).first()
                if rule is not None:
                    published += cls._evaluate(rule, clock())
        return published

    @staticmethod
    def _consume(rule, quote):
        rule.last_observation_id = quote.observation_id
        rule.generation = quote.generation
        rule.last_source_at = quote.source_at

    @classmethod
    def _suspend(cls, rule, reason, quote=None):
        changed = rule.last_condition is not None or rule.state_reason != reason or rule.baseline_at is not None
        rule.last_condition = None
        rule.state_reason = reason
        rule.baseline_at = None
        if quote is not None and (rule.last_source_at is None or quote.source_at > rule.last_source_at):
            cls._consume(rule, quote)
            changed = True
        if changed:
            rule.save(update_fields=STATE_FIELDS)
        return 0

    @classmethod
    def _evaluate(cls, rule, now):
        instrument = rule.instrument
        quote = Quote.objects.filter(instrument=instrument).first()
        if not AuthorizationPolicy.allows(
            rule.user, "alerts.manage", owner_id=rule.user_id,
            market=instrument.market, currency=instrument.currency,
        ):
            return cls._suspend(rule, "policy_denied", quote)
        if quote is None:
            return cls._suspend(rule, "missing_quote")
        reason = AlertObservationPolicy.invalid_reason(instrument, quote, now)
        if reason:
            # A future timestamp must not poison the durable high-water mark.
            return cls._suspend(rule, reason, None if reason == "future_observation" else quote)
        # Invalid persisted configuration (including a changed instrument cadence) fails closed.
        try:
            AlertConfigurationPolicy.validate(
                instrument=instrument, kind=rule.kind, threshold=rule.threshold,
                window_seconds=rule.window_seconds, cooldown_seconds=rule.cooldown_seconds,
                enabled=rule.enabled,
            )
        except DomainError:
            return cls._suspend(rule, "invalid_configuration", quote)
        if rule.last_source_at is not None and quote.source_at <= rule.last_source_at:
            if quote.generation != rule.generation:
                return cls._suspend(rule, "generation_changed")
            return 0
        if quote.generation == rule.generation and quote.observation_id == rule.last_observation_id:
            return 0
        recovering = rule.last_condition is None or quote.generation != rule.generation
        if rule.last_source_at is not None:
            recovering |= quote.source_at - rule.last_source_at > AlertObservationPolicy.maximum_age(instrument)
        baseline = None
        if rule.kind not in PRICE_KINDS:
            baseline = AlertHistorySelector.baseline(instrument, quote, rule.window_seconds)
            if baseline is None:
                return cls._suspend(rule, "insufficient_history", quote)
            if baseline.price_units <= 0:
                return cls._suspend(rule, "invalid_baseline", quote)
        condition, value = cls._condition(rule, quote, baseline)
        crossing = not recovering and rule.last_condition is False and condition
        cooldown_ready = rule.last_triggered_at is None or (
            now >= rule.last_triggered_at + timedelta(seconds=rule.cooldown_seconds)
        )
        cls._consume(rule, quote)
        rule.last_condition = condition
        rule.baseline_at = baseline.source_at if baseline is not None else None
        rule.state_reason = "initialized" if recovering else ("condition_met" if condition else "watching")
        published = 0
        if crossing and cooldown_ready:
            identity = f"{quote.generation}:{quote.observation_id}"
            digest = hashlib.sha256(identity.encode()).hexdigest()
            notification = NotificationPublisher.publish(
                rule.user, f"alerts.{rule.kind.lower()}",
                {
                    "symbol": instrument.symbol, "value": value, "threshold": rule.threshold,
                    "baseline_at": baseline.source_at.isoformat() if baseline is not None else "",
                    "window": str(rule.window_seconds) if rule.window_seconds is not None else "",
                },
                f"alerts:{rule.id}:{digest}",
            )
            rule.last_triggered_at = now
            rule.state_reason = "triggered"
            AuditRecorder.record(
                None, "alerts.triggered", "alert_rule", rule.id,
                changes={"notification_id": str(notification.id), "user_id": rule.user_id,
                         "observation_id": quote.observation_id},
            )
            published = 1
        elif crossing:
            # Persist true even when suppressed. Cooldown expiry alone must never deliver.
            rule.state_reason = "cooldown"
        rule.save(update_fields=STATE_FIELDS)
        return published

    @staticmethod
    def _condition(rule, quote, baseline):
        with localcontext() as context:
            context.prec = 64
            current = Decimal(quote.price_units).scaleb(-rule.instrument.price_scale)
            threshold = Decimal(rule.threshold)
            if rule.kind == AlertRule.Kind.PRICE_ABOVE:
                return current > threshold, canonical_decimal(current)
            if rule.kind == AlertRule.Kind.PRICE_BELOW:
                return current < threshold, canonical_decimal(current)
            previous = Decimal(baseline.price_units).scaleb(-rule.instrument.price_scale)
            movement = (current - previous) * 100
            # Compare without division/rounding at the exact boundary.
            if rule.kind == AlertRule.Kind.PERCENT_DROP:
                movement = -movement
            condition = movement >= threshold * previous
            value = (movement / previous).quantize(Decimal("0.00000001"))
            return condition, canonical_decimal(value)
