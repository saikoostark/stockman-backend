from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.markets.models import PriceObservation, ProviderHealth
from apps.wallets.money import Money, checked_units

CAIRO = ZoneInfo("Africa/Cairo")


class QuoteEligibility:
    @staticmethod
    def freshness_seconds(instrument):
        grace = max(0, getattr(settings, "EGX_PUBLICATION_GRACE_SECONDS", 3600)) if instrument.market == "EGX" else 0
        return max(getattr(settings, "QUOTE_MAX_AGE_SECONDS", 30), instrument.cadence_seconds * 2) + grace

    @classmethod
    def suitable(cls, instrument, quote, now) -> bool:
        if quote is None or not instrument.enabled or not quote.tradable:
            return False
        if (instrument.market, instrument.currency) not in {("CRYPTO", "USDT"), ("EGX", "EGP")}:
            return False
        # No documented exchange-session calendar exists for EGX intraday yet.
        if instrument.market == "EGX" and instrument.data_mode != "EOD":
            return False
        if not 0 < quote.price_units <= 2**63 - 1 or not quote.observation_id or not quote.generation:
            return False
        if not 0 <= instrument.price_scale <= 18 or not 0 <= instrument.quantity_scale <= 18:
            return False
        if instrument.quantity_step <= 0 or instrument.minimum_quantity <= 0:
            return False
        if instrument.delay_seconds < 0 or instrument.cadence_seconds <= 0:
            return False
        if quote.source_at > quote.received_at or quote.received_at > now:
            return False
        freshness = cls.freshness_seconds(instrument)
        if (now - quote.received_at).total_seconds() > freshness:
            return False
        if (now - quote.source_at).total_seconds() > instrument.delay_seconds + freshness:
            return False
        health = ProviderHealth.objects.filter(provider=instrument.provider).first()
        if health is None or health.status != "AVAILABLE" or str(health.generation) != str(quote.generation):
            return False
        if health.last_success_at is None or health.last_success_at > now or (now - health.last_success_at).total_seconds() > freshness:
            return False
        if instrument.data_mode == "EOD":
            # BAR means a completed session in the normalized provider contract. No OHLC crossing.
            if not PriceObservation.objects.filter(
                instrument=instrument, generation=quote.generation,
                observation_id=quote.observation_id, source_at=quote.source_at,
                price_units=quote.price_units, kind="BAR", tradable=True,
            ).exists():
                return False
        return True

    @classmethod
    def require_acceptance(cls, instrument, quote, now):
        if not cls.suitable(instrument, quote, now):
            raise DomainError("market_unavailable", _("A healthy, fresh, tradable observation is required."), status=409)

    @classmethod
    def eligible(cls, order, quote, now) -> bool:
        instrument = order.instrument
        if not cls.suitable(instrument, quote, now):
            return False
        if (order.currency, order.price_scale, order.quantity_scale, order.provider, order.data_mode) != (
            instrument.currency, instrument.price_scale, instrument.quantity_scale, instrument.provider, instrument.data_mode
        ):
            return False
        if str(quote.generation) != order.generation or quote.observation_id in {order.source_watermark, order.last_watermark}:
            return False
        if quote.source_at <= max(order.accepted_at, order.source_at, order.last_source_at):
            return False
        if quote.received_at <= order.accepted_at:
            return False
        if instrument.data_mode == "EOD" and quote.source_at.astimezone(CAIRO).date() <= order.accepted_at.astimezone(CAIRO).date():
            return False
        return True

    @staticmethod
    def deadline(instrument, now):
        base = max(1, getattr(settings, "MARKET_ORDER_TIMEOUT_SECONDS", 60))
        if instrument.data_mode == "EOD":
            # Conservative publication deadline, NOT an invented exchange calendar.
            # Eligibility still requires a real, newly completed later session.
            seconds = max(getattr(settings, "EOD_MARKET_ORDER_TIMEOUT_SECONDS", 7 * 86400),
                          instrument.delay_seconds + instrument.cadence_seconds * 2)
            seconds += max(0, getattr(settings, "EGX_PUBLICATION_GRACE_SECONDS", 3600))
        elif instrument.data_mode == "DELAYED":
            seconds = max(base, instrument.delay_seconds + instrument.cadence_seconds * 2
                          + getattr(settings, "EGX_PUBLICATION_GRACE_SECONDS", 3600))
        else:
            seconds = base
        return now + timedelta(seconds=seconds)


class ExecutionRules:
    @staticmethod
    def validate_quantity(instrument, quantity: int, price: int):
        checked_units(quantity, positive=True)
        if quantity % instrument.quantity_step or quantity < instrument.minimum_quantity:
            raise DomainError("invalid_quantity", _("Quantity must satisfy the instrument minimum and step."))
        # Minimum notional compares exact value, not a rounded-up purchase cost.
        if quantity * price * 10**Money.scale(instrument.currency) < instrument.minimum_notional * 10 ** (instrument.price_scale + instrument.quantity_scale):
            raise DomainError("minimum_notional", _("Order is below the minimum notional."))

    @staticmethod
    def crosses(order, price: int) -> bool:
        if order.order_type == "MARKET":
            return True
        return price <= order.limit_price_units if order.side == "BUY" else price >= order.limit_price_units

    @staticmethod
    def cash(instrument, price: int, quantity: int, *, buy: bool) -> int:
        amount = Money.cost(instrument.currency, price, quantity,
                            instrument.price_scale, instrument.quantity_scale, round_up=buy)
        checked_units(amount.units, positive=True)
        return amount.units
