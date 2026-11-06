import hashlib
import json
from dataclasses import asdict

from django.db import transaction
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.markets.dtos import CatalogCommand, InstrumentData, WatchCommand
from apps.markets.models import Instrument, PriceObservation, Quote, Watchlist


class CatalogSync:
    # Changes to any financial/source interpretation require operator intervention.
    RULE_FIELDS = (
        "market", "currency", "provider", "provider_symbol", "price_scale", "quantity_scale",
        "price_step", "quantity_step", "minimum_quantity", "minimum_notional", "data_mode",
        "delay_seconds", "venue_timezone",
    )

    @classmethod
    def apply(cls, provider: str, items: tuple[InstrumentData, ...]):
        from apps.audit.services import AuditRecorder

        with transaction.atomic():
            seen = []
            for data in items:
                values = asdict(data)
                fingerprint = hashlib.sha256(json.dumps({k: values[k] for k in cls.RULE_FIELDS}, sort_keys=True).encode()).hexdigest()
                instrument = Instrument.objects.filter(market=data.market, symbol=data.symbol).first()
                if instrument is None:
                    instrument = Instrument(**values, metadata_fingerprint=fingerprint)
                    instrument.full_clean()
                    instrument.save()
                    AuditRecorder.record(None, "catalog.discovered", "Instrument", instrument.id,
                                         changes={"symbol": data.symbol, "provider": provider})
                elif any(getattr(instrument, key) != values[key] for key in cls.RULE_FIELDS):
                    cls.halt(instrument, "METADATA_CHANGED")
                elif not instrument.metadata_fingerprint:
                    instrument.metadata_fingerprint = fingerprint
                    instrument.save(update_fields=["metadata_fingerprint"])
                seen.append(instrument.id)
            for instrument in Instrument.objects.filter(provider=provider, enabled=True).exclude(id__in=seen):
                cls.halt(instrument, "SYMBOL_UNAVAILABLE")
            return seen

    @staticmethod
    def halt(instrument, reason):
        from apps.audit.services import AuditRecorder

        if instrument.halted_reason == reason and not instrument.enabled:
            return
        instrument.enabled = False
        instrument.halted_reason = reason
        instrument.save(update_fields=["enabled", "halted_reason"])
        Quote.objects.filter(instrument=instrument).update(tradable=False)
        PriceObservation.objects.filter(instrument=instrument, tradable=True).update(tradable=False)
        AuditRecorder.record(None, "catalog.halted", "Instrument", instrument.id, changes={"reason": reason})

    @staticmethod
    def data(instrument) -> InstrumentData:
        return InstrumentData(**{field: getattr(instrument, field) for field in InstrumentData.__dataclass_fields__})


class UpdateCatalog:
    @staticmethod
    def execute(actor, command: CatalogCommand, *, request_id=""):
        from apps.audit.services import AuditRecorder
        from apps.identity.policies import AuthorizationPolicy

        with transaction.atomic():
            instrument = Instrument.objects.filter(id=command.instrument_id).first()
            if instrument is None:
                raise DomainError("not_found", _("Instrument not found."), status=404)
            AuthorizationPolicy.require(actor, "catalog.manage", market=instrument.market, currency=instrument.currency)
            if command.enabled and instrument.halted_reason:
                raise DomainError("instrument_halted", _("Instrument metadata must be verified before enabling."), status=409)
            if command.name is not None and (not command.name.strip() or len(command.name) > 160):
                raise DomainError("invalid_name", _("Provide a valid instrument name."))
            changes = {}
            for field in ("name", "enabled", "watched"):
                value = getattr(command, field)
                if value is not None and value != getattr(instrument, field):
                    changes[field] = {"before": getattr(instrument, field), "after": value}
                    setattr(instrument, field, value)
            if changes:
                instrument.full_clean()
                instrument.save(update_fields=list(changes))
                if not instrument.enabled:
                    Quote.objects.filter(instrument=instrument).update(tradable=False)
                    PriceObservation.objects.filter(instrument=instrument, tradable=True).update(tradable=False)
                AuditRecorder.record(actor, "catalog.updated", "Instrument", instrument.id,
                                     changes=changes, request_id=request_id, reason=command.reason)
            from apps.markets.selectors import MarketSelectors

            return MarketSelectors().describe(instrument)


class ManageWatchlist:
    @staticmethod
    def add(actor, command: WatchCommand):
        from apps.identity.policies import AuthorizationPolicy

        with transaction.atomic():
            instrument = Instrument.objects.filter(id=command.instrument_id).first()
            if instrument is None:
                raise DomainError("not_found", _("Instrument not found."), status=404)
            AuthorizationPolicy.require(actor, "markets.read", owner_id=actor.pk,
                                        market=instrument.market, currency=instrument.currency)
            if Watchlist.objects.filter(user=actor).count() >= 100 and not Watchlist.objects.filter(user=actor, instrument=instrument).exists():
                raise DomainError("watchlist_full", _("Watchlists are limited to 100 instruments."))
            from apps.markets.selectors import MarketSelectors

            return MarketSelectors().watch(Watchlist.objects.get_or_create(user=actor, instrument=instrument)[0])

    @staticmethod
    def remove(actor, command: WatchCommand):
        from apps.identity.policies import AuthorizationPolicy

        with transaction.atomic():
            item = Watchlist.objects.select_related("instrument").filter(user=actor, instrument_id=command.instrument_id).first()
            AuthorizationPolicy.require(actor, "markets.read", owner_id=actor.pk,
                                        market=item.instrument.market if item else None,
                                        currency=item.instrument.currency if item else None)
            if item:
                item.delete()
