from datetime import timedelta

from django.conf import settings
from django.utils import timezone


class SourcePolicy:
    @staticmethod
    def max_age(instrument) -> timedelta:
        if instrument.data_mode == "EOD":
            seconds = instrument.cadence_seconds + getattr(settings, "EGX_PUBLICATION_GRACE_SECONDS", 3600)
        else:
            seconds = instrument.delay_seconds + getattr(settings, "QUOTE_MAX_AGE_SECONDS", 30)
        return timedelta(seconds=max(1, seconds))

    @classmethod
    def stale(cls, instrument, source_at, *, now=None) -> bool:
        now = now or timezone.now()
        return source_at > now or now - source_at > cls.max_age(instrument)
