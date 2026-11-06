from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID


class Unset(Enum):
    VALUE = "unset"


@dataclass(frozen=True, slots=True)
class CreateAlertCommand:
    instrument_id: UUID
    kind: str
    threshold: str | Decimal
    window_seconds: int | None = None
    cooldown_seconds: int | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class UpdateAlertCommand:
    instrument_id: UUID | Unset = Unset.VALUE
    kind: str | Unset = Unset.VALUE
    threshold: str | Decimal | Unset = Unset.VALUE
    window_seconds: int | None | Unset = Unset.VALUE
    cooldown_seconds: int | Unset = Unset.VALUE
    enabled: bool | Unset = Unset.VALUE


@dataclass(frozen=True, slots=True)
class DeleteAlertCommand:
    alert_id: UUID


@dataclass(frozen=True, slots=True)
class AlertDTO:
    id: UUID
    instrument_id: UUID
    symbol: str
    market: str
    currency: str
    kind: str
    threshold: str
    window_seconds: int | None
    cooldown_seconds: int
    enabled: bool
    last_condition: bool | None
    last_observation_id: str
    generation: UUID | None
    last_source_at: datetime | None
    last_triggered_at: datetime | None
    baseline_at: datetime | None
    state_reason: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, rule):
        return cls(
            id=rule.id, instrument_id=rule.instrument_id, symbol=rule.instrument.symbol,
            market=rule.instrument.market, currency=rule.instrument.currency,
            kind=rule.kind, threshold=rule.threshold, window_seconds=rule.window_seconds,
            cooldown_seconds=rule.cooldown_seconds, enabled=rule.enabled,
            last_condition=rule.last_condition, last_observation_id=rule.last_observation_id,
            generation=rule.generation, last_source_at=rule.last_source_at,
            last_triggered_at=rule.last_triggered_at, baseline_at=rule.baseline_at,
            state_reason=rule.state_reason, created_at=rule.created_at, updated_at=rule.updated_at,
        )


@dataclass(frozen=True, slots=True)
class DeletedAlertDTO:
    id: UUID
    deleted: bool = True
