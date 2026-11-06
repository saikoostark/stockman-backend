import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from django.db import transaction

from apps.markets.dtos import CandleData, InstrumentData, ProviderCapabilities, QuoteData
from apps.markets.models import Instrument, PriceObservation, ProviderHealth, ProviderUsage, Quote
from apps.markets.providers.validation import ProviderError
from apps.markets.services import MarketDataIngestor
from apps.markets.services.budget import ProviderBudget
from apps.markets.services.catalog import CatalogSync

pytestmark = pytest.mark.django_db(transaction=True)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 5, 20, 12, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds=11):
        self.value += timedelta(seconds=seconds)


class Feed:
    def __init__(self, clock):
        self.clock = clock
        self.data = InstrumentData("BTCUSDT", "BTC / USDT", "CRYPTO", "USDT", "BINANCE", "BTCUSDT", 2, 3, 1, 1, 500000000)
        self.quote = QuoteData("BTCUSDT", 10000, clock(), "t1")
        self.bars = ()
        self.error = None
        self.calls = 0
        self.on_fetch = None

    def capabilities(self):
        return ProviderCapabilities("BINANCE", "CRYPTO", "USDT", "UTC", "LIVE", 10, 0, 1000, False, True)

    def instruments(self):
        return (self.data,)

    def latest(self, instrument):
        from django.db import connection
        assert not connection.in_atomic_block
        self.calls += 1
        if self.on_fetch:
            self.on_fetch()
        if self.error:
            raise self.error
        return self.quote

    def history(self, instrument, *, limit=100):
        from django.db import connection
        assert not connection.in_atomic_block
        if self.error:
            raise self.error
        return self.bars

    def close(self):
        pass

    def advance(self, *, price=11000, seconds=11):
        self.clock.advance(seconds)
        self.quote = QuoteData("BTCUSDT", price, self.clock(), f"t:{self.clock().isoformat()}")


@pytest.fixture
def source(settings):
    settings.BINANCE_SYMBOLS = ["BTCUSDT"]
    settings.EGX_ACCESS_VERIFIED = False
    clock = Clock()
    feed = Feed(clock)
    worker = MarketDataIngestor((feed,), clock=clock)
    return clock, feed, worker


def test_startup_is_nontradable_then_genuinely_advancing_data_is_eligible(source):
    clock, feed, worker = source
    ids = worker.refresh()
    assert len(ids) == 1 and isinstance(ids[0], uuid.UUID)
    quote = Quote.objects.get()
    assert not quote.tradable
    assert PriceObservation.objects.count() == 1
    feed.advance()
    assert worker.refresh() == ids
    quote.refresh_from_db()
    assert quote.tradable
    assert quote.source_at == clock()
    assert quote.received_at == clock()


def test_repeated_and_out_of_order_prices_preserve_source_age(source):
    clock, feed, worker = source
    worker.refresh()
    original = Quote.objects.get()
    clock.advance()
    assert worker.refresh() == []
    clock.advance()
    feed.quote = replace(feed.quote, source_at=original.source_at - timedelta(seconds=2), observation_id="out-of-order")
    assert worker.refresh() == []
    current = Quote.objects.get()
    assert (current.source_at, current.received_at, current.observation_id) == (original.source_at, original.received_at, original.observation_id)
    assert PriceObservation.objects.count() == 1


def test_stale_tick_does_not_replace_current_quote(source):
    clock, feed, worker = source
    worker.refresh()
    before = Quote.objects.get()
    clock.advance(100)
    feed.quote = replace(feed.quote, observation_id="late", source_at=before.source_at + timedelta(seconds=1))
    assert worker.refresh() == []
    assert Quote.objects.get().received_at == before.received_at


def test_stale_initial_data_is_visible_but_never_eligible(source):
    clock, feed, worker = source
    feed.quote = replace(feed.quote, source_at=clock() - timedelta(hours=1))
    worker.refresh()
    assert not Quote.objects.get().tradable
    assert Quote.objects.get().source_at == feed.quote.source_at


def test_persistent_cadence_limits_polling_and_survives_restart(source):
    clock, feed, worker = source
    worker.refresh()
    worker.refresh()
    assert feed.calls == 1
    restarted = MarketDataIngestor((feed,), clock=clock)
    restarted.refresh()
    assert feed.calls == 1


def test_restart_recovers_a_new_generation_without_trading_recovered_price(source):
    clock, feed, worker = source
    worker.refresh()
    feed.advance()
    worker.refresh()
    old_generation = Quote.objects.get().generation
    feed.advance()
    restarted = MarketDataIngestor((feed,), clock=clock)
    restarted.refresh()
    current = Quote.objects.get()
    assert current.generation != old_generation
    assert not current.tradable
    assert not PriceObservation.objects.filter(tradable=True).exists()
    feed.advance()
    restarted.refresh()
    assert Quote.objects.get().tradable


def test_replaced_worker_cannot_commit_late_http_response(source):
    clock, feed, worker = source
    worker.refresh()
    feed.advance()
    successor = MarketDataIngestor((Feed(clock),), clock=clock)
    feed.on_fetch = lambda: successor.refresh()
    assert worker.refresh() == []
    assert ProviderHealth.objects.get().generation == successor.generation
    assert Quote.objects.get().price_units == 10000


def test_failures_backoff_and_recovery_bootstraps_before_trading(source):
    clock, feed, worker = source
    worker.refresh()
    feed.advance()
    worker.refresh()
    feed.advance()
    feed.error = ProviderError("RATE_LIMITED", retry_after=120)
    assert worker.refresh() == []
    assert not Quote.objects.get().tradable
    assert ProviderHealth.objects.get().status == "BACKOFF"
    calls = feed.calls
    feed.advance()
    worker.refresh()
    assert feed.calls == calls
    feed.error = None
    feed.advance(seconds=121)
    worker.refresh()
    assert not Quote.objects.get().tradable
    feed.advance()
    worker.refresh()
    assert Quote.objects.get().tradable
    assert ProviderHealth.objects.get().status == "AVAILABLE"


def test_metadata_scale_change_halts_without_mixing_history(source):
    clock, feed, worker = source
    worker.refresh()
    instrument = Instrument.objects.get()
    CatalogSync.apply("BINANCE", (replace(feed.data, price_scale=3),))
    instrument.refresh_from_db()
    assert not instrument.enabled
    assert instrument.halted_reason == "METADATA_CHANGED"
    assert instrument.price_scale == 2
    assert Quote.objects.get().price_units == 10000


def test_historical_completed_bars_do_not_replace_quote_or_enable_trading(source):
    clock, feed, worker = source
    worker.refresh()
    instrument = Instrument.objects.get()
    original = Quote.objects.get()
    feed.bars = (CandleData("BTCUSDT", "bar:1", "1m", clock() - timedelta(minutes=2), clock() - timedelta(minutes=1), 8000, 10000, 7000, 9000, 1000),)
    assert worker.backfill(instrument.id) == 1
    assert worker.backfill(instrument.id) == 0
    assert PriceObservation.objects.filter(kind="BAR", tradable=False).count() == 1
    assert Quote.objects.get().observation_id == original.observation_id


def test_retention_is_bounded_by_source_order(source):
    clock, feed, worker = source
    worker.history_limit = 2
    worker.refresh()
    for _ in range(3):
        feed.advance()
        worker.refresh()
    assert PriceObservation.objects.count() == 2
    assert PriceObservation.objects.first().source_at == clock()


def test_quote_and_history_persist_atomically(source, monkeypatch):
    clock, feed, worker = source
    worker.refresh()
    old = Quote.objects.get()
    feed.advance()
    def broken(*args, **kwargs):
        raise RuntimeError("database write interrupted")
    monkeypatch.setattr(PriceObservation.objects, "create", broken)
    with pytest.raises(RuntimeError):
        worker.refresh()
    assert Quote.objects.get().source_at == old.source_at


def test_ingestor_forbids_network_under_transaction(source):
    with transaction.atomic(), pytest.raises(RuntimeError, match="outside"):
        source[2].refresh()


def test_daily_egx_quota_is_persistent_and_clamped_to_twenty(settings):
    settings.EODHD_DAILY_CALL_LIMIT = 100
    clock = Clock()
    budget = ProviderBudget(clock=clock)
    for _ in range(20):
        budget.reserve("EODHD")
    with pytest.raises(ProviderError, match="LOCAL_QUOTA_EXHAUSTED"):
        ProviderBudget(clock=clock).reserve("EODHD")
    assert ProviderUsage.objects.get().used == 20
    clock.advance(86400)
    budget.reserve("EODHD")
    assert ProviderUsage.objects.count() == 2


def test_binance_weight_reservation_enforces_shared_window(settings):
    settings.BINANCE_REQUEST_WEIGHT_PER_MINUTE = 24
    clock = Clock()
    ProviderBudget(clock=clock).reserve("BINANCE", 20)
    ProviderBudget(clock=clock).reserve("BINANCE", 4)
    with pytest.raises(ProviderError):
        ProviderBudget(clock=clock).reserve("BINANCE", 1)
    clock.advance(60)
    ProviderBudget(clock=clock).reserve("BINANCE", 20)


def test_egx_disabled_health_is_explicit_without_calls(settings):
    from apps.markets.providers.eodhd import EODHDProvider
    calls = []
    provider = EODHDProvider(verified=False, token="", symbols=[], reserve=lambda *_: calls.append(1))
    worker = MarketDataIngestor((provider,))
    assert worker.refresh() == []
    health = ProviderHealth.objects.get(provider="EODHD")
    assert health.status == "UNAVAILABLE"
    assert health.reason == "EGX_ACCESS_NOT_VERIFIED"
    assert not Quote.objects.exists()
    assert calls == []
    worker.close()
