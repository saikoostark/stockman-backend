from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: str
    market: str
    currency: str
    venue_timezone: str
    data_mode: str
    cadence_seconds: int
    delay_seconds: int
    history_limit: int
    entitlement_verified: bool
    available: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class InstrumentData:
    symbol: str
    name: str
    market: str
    currency: str
    provider: str
    provider_symbol: str
    price_scale: int
    quantity_scale: int
    quantity_step: int
    minimum_quantity: int
    minimum_notional: int
    price_step: int = 1
    data_mode: str = "LIVE"
    delay_seconds: int = 0
    cadence_seconds: int = 10
    venue_timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class QuoteData:
    provider_symbol: str
    price_units: int
    source_at: datetime
    observation_id: str
    # Date-only sources can only become eligible as newly completed sessions.
    session_date: str | None = None


@dataclass(frozen=True, slots=True)
class CandleData:
    provider_symbol: str
    observation_id: str
    interval: str
    opened_at: datetime
    closed_at: datetime
    open_units: int
    high_units: int
    low_units: int
    close_units: int
    volume_units: int


class MarketDataProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def instruments(self) -> tuple[InstrumentData, ...]: ...
    def latest(self, instrument: InstrumentData) -> QuoteData: ...
    def history(self, instrument: InstrumentData, *, limit: int = 100) -> tuple[CandleData, ...]: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class InstrumentFilter:
    market: str = ""
    currency: str = ""
    search: str = ""
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class WatchCommand:
    instrument_id: UUID


@dataclass(frozen=True, slots=True)
class CatalogCommand:
    instrument_id: UUID
    name: str | None = None
    enabled: bool | None = None
    watched: bool | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class QuoteView:
    price: str
    source: str
    source_at: datetime
    received_at: datetime
    observation_id: str
    generation: UUID
    tradable: bool
    stale: bool
    delay_seconds: int
    currency: str


@dataclass(frozen=True, slots=True)
class InstrumentView:
    id: UUID
    symbol: str
    name: str
    market: str
    currency: str
    provider: str
    price_scale: int
    quantity_scale: int
    price_step: str
    quantity_step: str
    minimum_quantity: str
    minimum_notional: str
    enabled: bool
    watched: bool
    data_mode: str
    delay_seconds: int
    cadence_seconds: int
    venue_timezone: str
    availability: str
    reason: str
    quote: QuoteView | None


@dataclass(frozen=True, slots=True)
class ObservationView:
    id: int
    price: str
    source: str
    source_at: datetime
    received_at: datetime
    currency: str
    data_mode: str
    delay_seconds: int
    observation_id: str
    generation: UUID
    tradable: bool
    kind: str


@dataclass(frozen=True, slots=True)
class WatchView:
    instrument: InstrumentView
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderView:
    provider: str
    market: str
    currency: str
    status: str
    reason: str
    message: str
    data_mode: str
    venue_timezone: str
    delay_seconds: int
    cadence_seconds: int
    history_limit: int
    entitlement_verified: bool
    last_success_at: datetime | None
    retry_at: datetime | None
