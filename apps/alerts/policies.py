"""Alert input and observation rules; all arithmetic stays outside SQLite."""

import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError

DAY_SECONDS = 86400
# Bounded calendar tolerance for EOD weekends/publication gaps, not intraday data.
EOD_CALENDAR_TOLERANCE_SECONDS = 4 * DAY_SECONDS
MAX_COOLDOWN_SECONDS = 365 * DAY_SECONDS
MAX_THRESHOLD = Decimal("92233720368.54775807")
PRICE_KINDS = frozenset({"PRICE_ABOVE", "PRICE_BELOW"})
PERCENT_KINDS = frozenset({"PERCENT_RISE", "PERCENT_DROP"})


def canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def default_cooldown() -> int:
    return settings.ALERT_COOLDOWN_SECONDS


class AlertConfigurationPolicy:
    @staticmethod
    def invalid(field, message):
        raise DomainError("invalid_alert", message, fields={field: [message]})

    @classmethod
    def threshold(cls, value, kind):
        if not isinstance(value, (str, Decimal)):
            cls.invalid("threshold", _("Use a positive decimal string."))
        if isinstance(value, Decimal):
            # Decimal renders small valid numbers using exponents. Bound it before
            # fixed-point formatting so huge exponents cannot allocate huge strings.
            if not value.is_finite() or not 0 < value <= MAX_THRESHOLD:
                cls.invalid("threshold", _("The threshold is outside the supported range."))
            exponent = value.as_tuple().exponent
            if isinstance(exponent, int) and exponent < -8:
                cls.invalid("threshold", _("Use at most eight decimal places."))
            text = format(value, "f")
        else:
            text = str(value)
        if len(text) > 32 or not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,8})?", text):
            cls.invalid("threshold", _("Use at most eight decimal places without an exponent."))
        amount = Decimal(text)
        limit = Decimal("100") if kind == "PERCENT_DROP" else (
            Decimal("1000000") if kind == "PERCENT_RISE" else MAX_THRESHOLD
        )
        if not amount.is_finite() or not 0 < amount <= limit:
            cls.invalid("threshold", _("The threshold is outside the supported range."))
        return canonical_decimal(amount)

    @classmethod
    def validate(cls, *, instrument, kind, threshold, window_seconds, cooldown_seconds, enabled):
        if not isinstance(kind, str) or kind not in PRICE_KINDS | PERCENT_KINDS:
            cls.invalid("kind", _("Unknown alert kind."))
        threshold = cls.threshold(threshold, kind)
        if type(enabled) is not bool:
            cls.invalid("enabled", _("Use a boolean value."))
        if type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= MAX_COOLDOWN_SECONDS:
            cls.invalid("cooldown_seconds", _("The cooldown must be between zero and one year."))
        if kind in PRICE_KINDS:
            if window_seconds is not None:
                cls.invalid("window_seconds", _("Price alerts do not accept a rolling window."))
        else:
            minimum = max(1, instrument.cadence_seconds)
            if instrument.data_mode == "EOD":
                minimum = max(minimum, DAY_SECONDS)
            maximum = settings.MARKET_HISTORY_DAYS * DAY_SECONDS
            if type(window_seconds) is not int or not minimum <= window_seconds <= maximum:
                cls.invalid("window_seconds", _("The window must fit the instrument cadence and retained history."))
            if instrument.data_mode == "EOD" and window_seconds % DAY_SECONDS:
                cls.invalid("window_seconds", _("End-of-day alerts require a whole number of calendar days."))
        return threshold


class AlertObservationPolicy:
    @staticmethod
    def baseline_tolerance(instrument):
        seconds = instrument.cadence_seconds + instrument.delay_seconds
        if instrument.data_mode == "EOD":
            seconds = max(seconds, EOD_CALENDAR_TOLERANCE_SECONDS + instrument.delay_seconds)
            seconds += settings.EGX_PUBLICATION_GRACE_SECONDS
        return timedelta(seconds=seconds)

    @staticmethod
    def maximum_age(instrument):
        seconds = max(settings.QUOTE_MAX_AGE_SECONDS, instrument.cadence_seconds)
        if instrument.data_mode == "EOD":
            seconds = max(seconds, EOD_CALENDAR_TOLERANCE_SECONDS)
            seconds += settings.EGX_PUBLICATION_GRACE_SECONDS
        return timedelta(seconds=seconds + instrument.delay_seconds)

    @classmethod
    def invalid_reason(cls, instrument, quote, now):
        if not instrument.enabled:
            return "instrument_disabled"
        if quote is None:
            return "missing_quote"
        if not quote.tradable:
            return "nontradable"
        if quote.price_units <= 0:
            return "invalid_price"
        if quote.source_at > quote.received_at or quote.received_at > now:
            return "future_observation"
        maximum_age = cls.maximum_age(instrument)
        received_age = maximum_age - timedelta(seconds=instrument.delay_seconds)
        if now - quote.source_at > maximum_age or now - quote.received_at > received_age:
            return "stale_quote"
        return None
