from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.markets.models import ProviderUsage
from apps.markets.providers.validation import ProviderError


class ProviderBudget:
    """One persisted quota shared by polling and historical backfill, never per browser."""

    def __init__(self, *, clock=timezone.now):
        self.clock = clock

    def reserve(self, provider: str, weight: int = 1):
        now = self.clock()
        if provider == "EODHD":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            limit = min(20, max(0, getattr(settings, "EODHD_DAILY_CALL_LIMIT", 20)))
        elif provider == "BINANCE":
            start = now.replace(second=0, microsecond=0)
            end = start + timedelta(minutes=1)
            limit = min(6000, max(0, getattr(settings, "BINANCE_REQUEST_WEIGHT_PER_MINUTE", 120)))
        else:
            raise ProviderError("UNKNOWN_PROVIDER")
        if type(weight) is not int or weight < 1:
            raise ProviderError("INVALID_WEIGHT")
        with transaction.atomic():
            usage, _ = ProviderUsage.objects.get_or_create(provider=provider, window_start=start)
            if usage.used + weight > limit:
                raise ProviderError("LOCAL_QUOTA_EXHAUSTED", retry_after=max(1, int((end - now).total_seconds()) + 1))
            usage.used += weight
            usage.save(update_fields=["used"])
            ProviderUsage.objects.filter(provider=provider, window_start__lt=start - timedelta(days=2)).delete()
