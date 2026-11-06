from typing import Any

from django.utils import timezone

from apps.identity.policies import AuthorizationPolicy
from apps.markets.models import Quote
from apps.trading.dtos import HoldingDTO, PortfolioCurrencyDTO
from apps.trading.models import Fill, Order, Position
from apps.trading.policies.execution import QuoteEligibility
from apps.wallets.money import Money, decimal_string


def display_total(currency, units):
    # Aggregates are not stored integers and may legitimately exceed a single row's range.
    scale = Money.scale(currency)
    whole, fraction = divmod(abs(units), 10**scale)
    return f"{'-' if units < 0 else ''}{whole}.{fraction:0{scale}d}"


class TradingSelector:
    @staticmethod
    def orders(actor):
        return AuthorizationPolicy.scope(actor, "trading.read", Order.objects.filter(user=actor).select_related("instrument"), market_field="instrument__market", currency_field="currency")

    @staticmethod
    def fills(actor):
        return AuthorizationPolicy.scope(actor, "trading.read", Fill.objects.filter(order__user=actor).select_related("order__instrument"), owner_field="order__user_id", market_field="order__instrument__market", currency_field="order__currency")

    @staticmethod
    def portfolio(actor, *, now=None) -> tuple[PortfolioCurrencyDTO, ...]:
        now = now or timezone.now()
        positions = AuthorizationPolicy.scope(actor, "trading.read", Position.objects.filter(user=actor).select_related("instrument"), market_field="instrument__market", currency_field="currency").order_by("currency", "instrument__symbol")
        grouped: dict[str, dict[str, Any]] = {}
        for position in positions:
            currency = position.currency
            instrument = position.instrument
            quote = Quote.objects.filter(instrument=instrument).first()
            quantity = position.available_units + position.reserved_units
            compatible = (currency, position.quantity_scale) == (instrument.currency, instrument.quantity_scale)
            stale = not compatible or not QuoteEligibility.suitable(instrument, quote, now)
            value = unrealized = None
            price = source_at = None
            if compatible and quote is not None and quote.source_at <= now and quote.price_units > 0:
                price = decimal_string(quote.price_units, instrument.price_scale)
                source_at = quote.source_at.isoformat()
                # Liquidation mark uses the same conservative downward cash rounding as sales.
                value = quote.price_units * quantity * 10**Money.scale(currency) // 10 ** (instrument.price_scale + position.quantity_scale)
                unrealized = value - position.cost_basis_units
            elif quantity == 0:
                value = unrealized = 0
            holding = HoldingDTO(str(instrument.pk), instrument.symbol, currency,
                decimal_string(position.available_units, position.quantity_scale),
                decimal_string(position.reserved_units, position.quantity_scale),
                Money(currency, position.cost_basis_units).amount,
                Money(currency, position.realized_pnl_units).amount,
                price, display_total(currency, value) if value is not None else None,
                display_total(currency, unrealized) if unrealized is not None else None, source_at, stale)
            bucket = grouped.setdefault(currency, {"holdings": [], "realized": 0, "unrealized": 0, "value": 0, "missing": False, "stale": False})
            bucket["holdings"].append(holding)
            bucket["realized"] += position.realized_pnl_units
            bucket["value"] += value or 0
            bucket["unrealized"] += unrealized or 0
            bucket["missing"] |= value is None
            bucket["stale"] |= stale
        return tuple(PortfolioCurrencyDTO(currency,
            display_total(currency, bucket["realized"]),
            None if bucket["missing"] else display_total(currency, bucket["unrealized"]),
            None if bucket["missing"] else display_total(currency, bucket["value"]),
            bucket["stale"], tuple(bucket["holdings"])) for currency, bucket in grouped.items())
