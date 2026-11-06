"""Fail-closed parsing of untrusted provider values; never accept binary floats."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

MAX_UNITS = 2**63 - 1
NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,30})(?:\.[0-9]{1,18})?\Z", re.ASCII)


class ProviderError(Exception):
    """Only stable codes cross this boundary, never response bodies/URLs/tokens."""

    def __init__(self, code: str, *, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def decimal_value(value) -> Decimal:
    if isinstance(value, Decimal):
        value = format(value, "f")
    elif type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        raise ProviderError("INVALID_NUMBER")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ProviderError("INVALID_NUMBER") from None
    if not result.is_finite():
        raise ProviderError("INVALID_NUMBER")
    return result


def units(value, scale: int, *, positive: bool = True) -> int:
    if type(scale) is not int or not 0 <= scale <= 18:
        raise ProviderError("INVALID_PRECISION")
    number = decimal_value(value)
    # Tuple-based conversion avoids Decimal's ambient context rounding large values.
    sign, digits, exponent = number.as_tuple()
    coefficient = int("".join(map(str, digits)))
    power = int(exponent) + scale
    if power < 0:
        divisor = 10 ** (-power)
        if coefficient % divisor:
            raise ProviderError("INVALID_PRECISION")
        result = coefficient // divisor
    else:
        result = coefficient * 10**power
    if sign or result < int(positive) or result > MAX_UNITS:
        raise ProviderError("VALUE_OUT_OF_RANGE")
    return result


def step(value) -> tuple[int, int]:
    number = decimal_value(value)
    text = format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else str(number)
    scale = len(text.partition(".")[2])
    return scale, units(text or "0", scale)


def integer(value, *, minimum: int = 0, maximum: int = MAX_UNITS) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProviderError("INVALID_INTEGER")
    return value


def timestamp_ms(value, now: datetime) -> datetime:
    milliseconds = integer(value, minimum=946684800000, maximum=4102444800000)
    result = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
    if result > now + timedelta(seconds=2):
        raise ProviderError("FUTURE_TIMESTAMP")
    return result


def decimal_string(value: int, scale: int) -> str:
    sign = "-" if value < 0 else ""
    digits = str(abs(value)).zfill(scale + 1)
    return sign + (f"{digits[:-scale]}.{digits[-scale:]}" if scale else digits)
