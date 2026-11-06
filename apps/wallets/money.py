"""Exact signed-64-bit money and instrument arithmetic; never accept floats."""
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError

MAX_UNITS = 2**63 - 1
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)


def checked_units(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or abs(value) > MAX_UNITS or (positive and value <= 0):
        raise DomainError("invalid_amount", _("Amount is outside the supported range."))
    return value


def to_units(value: str | Decimal, scale: int, *, positive: bool = True) -> int:
    if type(scale) is not int or not 0 <= scale <= 18:
        raise DomainError("invalid_precision", _("Unsupported financial precision."))
    if isinstance(value, Decimal):
        if not value.is_finite() or abs(value.adjusted()) > 100:
            raise DomainError("invalid_amount", _("Enter a finite decimal amount."))
        value = format(value, "f")
    if not isinstance(value, str) or len(value) > 100 or not _DECIMAL.fullmatch(value):
        raise DomainError("invalid_amount", _("Enter a positive decimal string."))
    whole, _separator, fraction = value.partition(".")
    if len(fraction) > scale:
        raise DomainError("invalid_precision", _("Amount has too many decimal places."))
    return checked_units(int(whole) * 10**scale + int(fraction.ljust(scale, "0") or "0"), positive=positive)


def decimal_string(units: int, scale: int) -> str:
    checked_units(units)
    sign = "-" if units < 0 else ""
    whole, fraction = divmod(abs(units), 10**scale)
    return f"{sign}{whole}.{fraction:0{scale}d}" if scale else f"{sign}{whole}"


@dataclass(frozen=True, slots=True)
class Money:
    currency: str
    units: int
    SCALES: ClassVar[dict[str, int]] = {"EGP": 2, "USDT": 8}

    def __post_init__(self):
        self.scale(self.currency)
        checked_units(self.units)

    @classmethod
    def scale(cls, currency: str) -> int:
        if currency not in cls.SCALES:
            raise DomainError("unsupported_currency", _("Unsupported wallet currency."))
        return cls.SCALES[currency]

    @classmethod
    def from_decimal(cls, currency: str, amount: str | Decimal, *, positive=True):
        return cls(currency, to_units(amount, cls.scale(currency), positive=positive))

    @property
    def amount(self) -> str:
        return decimal_string(self.units, self.scale(self.currency))

    def to_decimal(self) -> Decimal:
        return Decimal(self.amount)

    @classmethod
    def cost(cls, currency: str, price_units: int, quantity_units: int,
             price_scale: int, quantity_scale: int, *, round_up: bool):
        checked_units(price_units, positive=True)
        checked_units(quantity_units, positive=True)
        if not 0 <= price_scale <= 18 or not 0 <= quantity_scale <= 18:
            raise DomainError("invalid_precision", _("Unsupported financial precision."))
        numerator = price_units * quantity_units * 10**cls.scale(currency)
        denominator = 10 ** (price_scale + quantity_scale)
        units = (numerator + denominator - 1) // denominator if round_up else numerator // denominator
        return cls(currency, checked_units(units))

    @classmethod
    def affordable_quantity(cls, currency: str, budget_units: int, price_units: int,
                            price_scale: int, quantity_scale: int, step: int) -> int:
        checked_units(budget_units, positive=True)
        checked_units(price_units, positive=True)
        checked_units(step, positive=True)
        quantity = budget_units * 10 ** (price_scale + quantity_scale) // (
            price_units * 10**cls.scale(currency)
        )
        return checked_units(min(quantity, MAX_UNITS) // step * step)
