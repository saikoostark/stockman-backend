from dataclasses import dataclass
from datetime import datetime

from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.identity.policies import AuthorizationPolicy
from apps.markets.dtos import (
    InstrumentFilter,
    InstrumentView,
    ObservationView,
    ProviderView,
    QuoteView,
    WatchView,
)
from apps.markets.models import (
    CompletedCandle,
    Instrument,
    PriceObservation,
    ProviderHealth,
    Watchlist,
)
from apps.markets.policies import SourcePolicy
from apps.markets.providers import configured_capabilities
from apps.markets.providers.validation import decimal_string


@dataclass(frozen=True, slots=True)
class CandleView:
    source: str
    opened_at: datetime
    closed_at: datetime
    interval: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    currency: str
    delay_seconds: int
    complete: bool = True
    tradable: bool = False


class MarketSelectors:
    def __init__(self):
        self.health = {row.provider: row for row in ProviderHealth.objects.all()}
        self.capabilities = {caps.provider: caps for caps in configured_capabilities()}

    @staticmethod
    def instruments(actor, filters: InstrumentFilter | None = None):
        filters = filters or InstrumentFilter()
        query = AuthorizationPolicy.scope(actor, "markets.read", Instrument.objects.select_related("quote"),
                                          owner_field=None, market_field="market", currency_field="currency")
        if filters.market:
            query = query.filter(market=filters.market)
        if filters.currency:
            query = query.filter(currency=filters.currency)
        if filters.search:
            query = query.filter(symbol__icontains=filters.search) | query.filter(name__icontains=filters.search)
        if filters.enabled is not None:
            query = query.filter(enabled=filters.enabled)
        return query.order_by("market", "symbol", "id")

    @classmethod
    def instrument(cls, actor, instrument_id):
        instrument = cls.instruments(actor).filter(id=instrument_id).first()
        if instrument is None:
            raise DomainError("not_found", _("Instrument not found."), status=404)
        return instrument

    @staticmethod
    def _message(reason):
        if reason.startswith("EGX_") or reason == "INVALID_EGX_SYMBOLS":
            return _("EGX data is disabled: verified entitlement, permitted display rights, a token and configured symbols are required. Free EGX coverage has not been verified by this application.")
        if reason in {"CORPORATE_ACTION_UNSUPPORTED", "METADATA_CHANGED", "INVALID_PRECISION", "INVALID_PRICE_STEP"}:
            return _("Trading is halted because the source metadata or price adjustment changed. Corporate actions and incompatible history require manual review.")
        if reason:
            return _("Market data is unavailable or temporarily delayed. No replacement prices are generated.")
        return _("Public spot data only; deployment-region access and permitted use remain the operator's responsibility.")

    def availability(self, provider):
        caps = self.capabilities.get(provider)
        health = self.health.get(provider)
        if caps is None or not caps.available:
            return "UNAVAILABLE", caps.reason if caps else "UNKNOWN_PROVIDER"
        if health is None:
            return "UNAVAILABLE", "NOT_STARTED"
        return health.status, health.reason

    def describe(self, item):
        status, reason = self.availability(item.provider)
        if not item.enabled or item.halted_reason:
            status, reason = "UNAVAILABLE", item.halted_reason or "INSTRUMENT_DISABLED"
        quote = getattr(item, "quote", None)
        quote_view = None
        if quote:
            stale = SourcePolicy.stale(item, quote.source_at)
            quote_view = QuoteView(decimal_string(quote.price_units, item.price_scale), item.provider,
                                   quote.source_at, quote.received_at, quote.observation_id, quote.generation,
                                   quote.tradable and not stale and status == "AVAILABLE", stale,
                                   item.delay_seconds, item.currency)
        return InstrumentView(item.id, item.symbol, item.name, item.market, item.currency, item.provider,
                              item.price_scale, item.quantity_scale, decimal_string(item.price_step, item.price_scale),
                              decimal_string(item.quantity_step, item.quantity_scale),
                              decimal_string(item.minimum_quantity, item.quantity_scale),
                              decimal_string(item.minimum_notional, 8 if item.currency == "USDT" else 2),
                              item.enabled, item.watched, item.data_mode, item.delay_seconds, item.cadence_seconds,
                              item.venue_timezone, status, reason, quote_view)

    @classmethod
    def history(cls, actor, instrument_id, *, kind="observations", before=None):
        instrument = cls.instrument(actor, instrument_id)
        model = CompletedCandle if kind == "candles" else PriceObservation
        time_field = "closed_at" if kind == "candles" else "source_at"
        query = model.objects.filter(instrument=instrument).select_related("instrument")
        if before:
            query = query.filter(**{f"{time_field}__lt": before})
        return query.order_by(f"-{time_field}", "-id")

    @staticmethod
    def observation(item):
        instrument = item.instrument
        return ObservationView(item.id, decimal_string(item.price_units, instrument.price_scale), instrument.provider,
                               item.source_at, item.received_at, instrument.currency, instrument.data_mode,
                               instrument.delay_seconds, item.observation_id, item.generation, item.tradable, item.kind)

    @staticmethod
    def candle(item):
        instrument = item.instrument
        values = (
            decimal_string(item.open_units, instrument.price_scale),
            decimal_string(item.high_units, instrument.price_scale),
            decimal_string(item.low_units, instrument.price_scale),
            decimal_string(item.close_units, instrument.price_scale),
        )
        return CandleView(instrument.provider, item.opened_at, item.closed_at, item.interval,
                          *values,
                          decimal_string(item.volume_units, instrument.quantity_scale), instrument.currency,
                          instrument.delay_seconds)

    @staticmethod
    def watchlist(actor):
        return AuthorizationPolicy.scope(actor, "markets.read", Watchlist.objects.filter(user=actor).select_related("instrument__quote"),
                                         owner_field="user_id", market_field="instrument__market", currency_field="instrument__currency")

    def watch(self, item):
        return WatchView(self.describe(item.instrument), item.created_at)

    def providers(self, actor):
        result = []
        for caps in self.capabilities.values():
            if not AuthorizationPolicy.allows(actor, "markets.read", market=caps.market, currency=caps.currency):
                continue
            status, reason = self.availability(caps.provider)
            health = self.health.get(caps.provider)
            result.append(ProviderView(caps.provider, caps.market, caps.currency, status, reason, self._message(reason),
                                       caps.data_mode, caps.venue_timezone, caps.delay_seconds, caps.cadence_seconds,
                                       caps.history_limit, caps.entitlement_verified,
                                       health.last_success_at if health else None, health.retry_at if health else None))
        return result
