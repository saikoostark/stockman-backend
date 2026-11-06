"""Central, fenced polling. HTTP is always outside persistence transactions."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.markets.dtos import MarketDataProvider
from apps.markets.models import CompletedCandle, Instrument, PriceObservation, ProviderHealth, Quote
from apps.markets.policies import SourcePolicy
from apps.markets.providers import configured_providers
from apps.markets.providers.validation import ProviderError, integer

from .budget import ProviderBudget
from .catalog import CatalogSync


class MarketDataIngestor:
    def __init__(self, providers: tuple[MarketDataProvider, ...] | None = None, *, clock=timezone.now, generation=None):
        self.clock = clock
        self.started_at = clock()
        self.generation = generation or uuid.uuid4()
        self.providers = providers if providers is not None else configured_providers(reserve=ProviderBudget(clock=clock).reserve, clock=clock)
        self.claimed: set[str] = set()
        self.bootstrapped: set[uuid.UUID] = set()
        self.catalogs: dict[str, list[uuid.UUID]] = {}
        self.catalog_times: dict[str, datetime] = {}
        self.batch_size = min(50, max(1, getattr(settings, "MARKET_POLL_BATCH_SIZE", 20)))
        self.history_limit = min(100000, max(2, getattr(settings, "MARKET_HISTORY_MAX_OBSERVATIONS", 10000)))

    def _claim(self, caps):
        with transaction.atomic():
            health, _ = ProviderHealth.objects.get_or_create(provider=caps.provider)
            if caps.provider not in self.claimed:
                health.generation = self.generation
                health.save(update_fields=["generation"])
                Quote.objects.filter(instrument__provider=caps.provider).update(tradable=False)
                PriceObservation.objects.filter(instrument__provider=caps.provider, tradable=True).update(tradable=False)
                self.claimed.add(caps.provider)
            return health.generation == self.generation

    def _health(self, provider, *, success=False, error=None, unavailable=False):
        now = self.clock()
        with transaction.atomic():
            health = ProviderHealth.objects.get(provider=provider)
            if health.generation != self.generation:
                return
            health.last_attempt_at = now
            if success:
                health.last_success_at = now
                health.status = ProviderHealth.Status.AVAILABLE
                health.reason = ""
                health.failures = 0
                health.retry_at = None
            else:
                health.failures = min(health.failures + 1, 30)
                health.reason = error.code
                health.status = ProviderHealth.Status.UNAVAILABLE if unavailable else ProviderHealth.Status.BACKOFF
                seconds = max(error.retry_after or 0, min(3600, 5 * 2 ** min(health.failures - 1, 10)))
                health.retry_at = now + timedelta(seconds=seconds)
                Quote.objects.filter(instrument__provider=provider).update(tradable=False)
                PriceObservation.objects.filter(instrument__provider=provider, tradable=True).update(tradable=False)
                self.bootstrapped.difference_update(Instrument.objects.filter(provider=provider).values_list("id", flat=True))
            health.save()

    def refresh(self) -> list[uuid.UUID]:
        if connection.in_atomic_block:
            raise RuntimeError("Market polling must run outside a database transaction.")
        refreshed = []
        for provider in self.providers:
            caps = provider.capabilities()
            if not self._claim(caps):
                continue
            if not caps.available:
                self._health(caps.provider, error=ProviderError(caps.reason), unavailable=True)
                continue
            health = ProviderHealth.objects.get(provider=caps.provider)
            if health.retry_at and health.retry_at > self.clock():
                continue
            try:
                now = self.clock()
                if caps.provider not in self.catalogs or now - self.catalog_times[caps.provider] >= timedelta(days=1):
                    metadata = provider.instruments()
                    if len(metadata) > 50 or any(item.provider != caps.provider for item in metadata):
                        raise ProviderError("INVALID_METADATA")
                    # Fence after HTTP: a replaced worker must not change catalog state.
                    with transaction.atomic():
                        if not ProviderHealth.objects.filter(provider=caps.provider, generation=self.generation).exists():
                            continue
                        self.catalogs[caps.provider] = CatalogSync.apply(caps.provider, metadata)
                        self.catalog_times[caps.provider] = now
                        ProviderHealth.objects.filter(provider=caps.provider).update(metadata_at=now)
                candidates = Instrument.objects.filter(
                    id__in=self.catalogs[caps.provider], provider=caps.provider, enabled=True, halted_reason="",
                ).filter(Q(watched=True) | Q(watchlists__isnull=False)).distinct().order_by("last_polled_at", "id")
                attempts = 0
                for instrument in candidates:
                    if attempts >= self.batch_size:
                        break
                    if not self._poll_slot(instrument):
                        continue
                    attempts += 1
                    quote = provider.latest(CatalogSync.data(instrument))
                    if self._persist(instrument.id, quote):
                        refreshed.append(instrument.id)
                    self._health(caps.provider, success=True)
                if not candidates.exists():
                    self._health(caps.provider, error=ProviderError("NO_SUPPORTED_INSTRUMENTS"), unavailable=True)
            except ProviderError as exc:
                if exc.code in {"CORPORATE_ACTION_UNSUPPORTED", "INVALID_PRECISION", "INVALID_PRICE_STEP"}:
                    with transaction.atomic():
                        if ProviderHealth.objects.filter(provider=caps.provider, generation=self.generation).exists():
                            for item in Instrument.objects.filter(provider=caps.provider, enabled=True):
                                CatalogSync.halt(item, exc.code)
                self._health(caps.provider, error=exc, unavailable=exc.code == "ACCESS_DENIED")
        return refreshed

    def _poll_slot(self, instrument):
        now = self.clock()
        with transaction.atomic():
            if not ProviderHealth.objects.filter(provider=instrument.provider, generation=self.generation).exists():
                return False
            # Persistent cadence survives worker restarts and browser traffic.
            cutoff = now - timedelta(seconds=max(5, instrument.cadence_seconds))
            return bool(Instrument.objects.filter(id=instrument.id, enabled=True, halted_reason="").filter(
                Q(last_polled_at__isnull=True) | Q(last_polled_at__lte=cutoff)
            ).update(last_polled_at=now))

    def _persist(self, instrument_id, data):
        now = self.clock()
        integer(data.price_units, minimum=1)
        if not isinstance(data.observation_id, str) or not 1 <= len(data.observation_id) <= 128 or not data.observation_id.isprintable():
            raise ProviderError("INVALID_OBSERVATION_ID")
        if not hasattr(data.source_at, "tzinfo") or timezone.is_naive(data.source_at) or data.source_at.year < 2000 or data.source_at > now:
            raise ProviderError("INVALID_SOURCE_TIME")
        with transaction.atomic():
            instrument = Instrument.objects.get(id=instrument_id)
            if not instrument.enabled or instrument.halted_reason or data.provider_symbol != instrument.provider_symbol:
                return False
            if not ProviderHealth.objects.filter(provider=instrument.provider, generation=self.generation).exists():
                return False
            if data.price_units % instrument.price_step:
                raise ProviderError("INVALID_PRICE_STEP")
            old = Quote.objects.filter(instrument=instrument).first()
            if old and (data.source_at <= old.source_at or data.observation_id == old.observation_id):
                # In particular, do not change received_at/generation when an old tick repeats.
                return False
            if SourcePolicy.stale(instrument, data.source_at, now=now):
                # Preserve an existing quote's source age; stale recovery is not new data.
                if old:
                    return False
            eligible = instrument.id in self.bootstrapped and old is not None and not SourcePolicy.stale(instrument, data.source_at, now=now)
            if instrument.data_mode == "EOD":
                local_date = data.source_at.astimezone(ZoneInfo(instrument.venue_timezone)).date()
                if data.session_date != local_date.isoformat() or local_date >= now.astimezone(ZoneInfo(instrument.venue_timezone)).date():
                    raise ProviderError("INVALID_SESSION_DATE")
                eligible = eligible and local_date > self.started_at.astimezone(ZoneInfo(instrument.venue_timezone)).date()
            else:
                eligible = eligible and data.source_at > self.started_at
            values = dict(price_units=data.price_units, source_at=data.source_at, received_at=now,
                          observation_id=data.observation_id, generation=self.generation, tradable=eligible)
            Quote.objects.update_or_create(instrument=instrument, defaults=values)
            PriceObservation.objects.create(instrument=instrument, **values)
            self._trim(instrument)
            self.bootstrapped.add(instrument.id)
            return True

    def _trim(self, instrument):
        cutoff = self.clock() - timedelta(days=min(366, max(1, getattr(settings, "MARKET_HISTORY_DAYS", 30))))
        for model, time_field in ((PriceObservation, "source_at"), (CompletedCandle, "closed_at")):
            query = model.objects.filter(instrument=instrument)
            query.filter(**{f"{time_field}__lt": cutoff}).delete()
            boundary = list(query.order_by(f"-{time_field}", "-id").values_list("id", flat=True)[self.history_limit - 1:self.history_limit])
            if boundary:
                # IDs alone need not follow source time after historical backfill.
                row = model.objects.get(id=boundary[0])
                value = getattr(row, time_field)
                query.filter(Q(**{f"{time_field}__lt": value}) | Q(**{time_field: value, "id__lt": row.id})).delete()

    def backfill(self, instrument_id, *, limit=100) -> int:
        """Explicit worker-only history loading; never called by chart API reads."""
        if connection.in_atomic_block:
            raise RuntimeError("Market backfill must run outside a database transaction.")
        integer(limit, minimum=1, maximum=1000)
        instrument = Instrument.objects.get(id=instrument_id)
        provider = next((p for p in self.providers if p.capabilities().provider == instrument.provider), None)
        if provider is None:
            raise ProviderError("UNKNOWN_PROVIDER")
        caps = provider.capabilities()
        if not caps.available:
            raise ProviderError(caps.reason)
        if not self._claim(caps):
            return 0
        health = ProviderHealth.objects.get(provider=caps.provider)
        if health.retry_at and health.retry_at > self.clock():
            raise ProviderError("PROVIDER_BACKOFF")
        try:
            bars = provider.history(CatalogSync.data(instrument), limit=limit)
            with transaction.atomic():
                instrument.refresh_from_db()
                if not instrument.enabled or instrument.halted_reason or not ProviderHealth.objects.filter(provider=instrument.provider, generation=self.generation).exists():
                    return 0
                count = 0
                for bar in bars:
                    if bar.provider_symbol != instrument.provider_symbol or bar.closed_at >= self.clock():
                        raise ProviderError("INVALID_BAR_TIME")
                    values = dict(source_id=bar.observation_id, interval=bar.interval,
                                  opened_at=bar.opened_at, closed_at=bar.closed_at, open_units=bar.open_units,
                                  high_units=bar.high_units, low_units=bar.low_units, close_units=bar.close_units,
                                  volume_units=bar.volume_units)
                    _, created = CompletedCandle.objects.get_or_create(instrument=instrument, generation=self.generation,
                                                                       source_id=bar.observation_id, defaults=values)
                    PriceObservation.objects.get_or_create(instrument=instrument, generation=self.generation,
                                                           observation_id=bar.observation_id,
                                                           defaults=dict(price_units=bar.close_units, source_at=bar.closed_at,
                                                                         received_at=self.clock(), tradable=False, kind="BAR"))
                    count += int(created)
                self._trim(instrument)
                return count
        except ProviderError as exc:
            if exc.code in {"CORPORATE_ACTION_UNSUPPORTED", "INVALID_PRECISION"}:
                with transaction.atomic():
                    if ProviderHealth.objects.filter(provider=caps.provider, generation=self.generation).exists():
                        CatalogSync.halt(instrument, exc.code)
            self._health(caps.provider, error=exc)
            raise

    def close(self):
        for provider in self.providers:
            provider.close()
