from datetime import timedelta

from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.identity.policies import AuthorizationPolicy
from apps.markets.models import Instrument, PriceObservation

from .dtos import AlertDTO
from .models import AlertRule
from .policies import AlertObservationPolicy


class AlertSelector:
    @staticmethod
    def for_user(user):
        return AuthorizationPolicy.scope(
            user, "alerts.read", AlertRule.objects.filter(user=user).select_related("instrument"),
            owner_field="user_id", market_field="instrument__market",
            currency_field="instrument__currency",
        )

    @staticmethod
    def owned(user, alert_id, action="alerts.read"):
        rule = AlertRule.objects.filter(user=user, pk=alert_id).select_related("instrument").first()
        if rule is None:
            raise DomainError("not_found", _("Alert not found."), status=404)
        AuthorizationPolicy.require(
            user, action, owner_id=rule.user_id, market=rule.instrument.market,
            currency=rule.instrument.currency,
        )
        return rule

    @classmethod
    def detail(cls, user, alert_id):
        return AlertDTO.from_model(cls.owned(user, alert_id))

    @staticmethod
    def instrument_for_management(user, instrument_id):
        instrument = Instrument.objects.filter(pk=instrument_id).first()
        if instrument is None:
            raise DomainError("not_found", _("Instrument not found."), status=404)
        AuthorizationPolicy.require(
            user, "alerts.manage", owner_id=user.pk, market=instrument.market,
            currency=instrument.currency,
        )
        return instrument


class AlertHistorySelector:
    @staticmethod
    def baseline(instrument, quote, window_seconds):
        target = quote.source_at - timedelta(seconds=window_seconds)
        earliest = target - AlertObservationPolicy.baseline_tolerance(instrument)
        # Same generation prevents mixing prices across metadata/scale discontinuities.
        # Backfill may establish a historical baseline, but never the current trigger.
        return PriceObservation.objects.filter(
            instrument=instrument, generation=quote.generation,
            source_at__lte=target, source_at__gte=earliest,
            received_at__lte=quote.received_at,
        ).order_by("-source_at", "-id").first()
