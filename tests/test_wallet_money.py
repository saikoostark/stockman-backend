from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from apps.common.exceptions import DomainError
from apps.wallets.money import MAX_UNITS, Money, checked_units, decimal_string, to_units
from apps.wallets.serializers import FundingSerializer


@pytest.mark.parametrize(("currency", "text", "units"), [
    ("EGP", "12.34", 1234), ("USDT", "0.00000001", 1), ("EGP", "1", 100),
    ("USDT", "92233720368.54775807", MAX_UNITS),
])
def test_money_roundtrip(currency, text, units):
    money = Money.from_decimal(currency, text)
    assert money.units == units
    assert Money.from_decimal(currency, money.amount) == money
    assert money.to_decimal() == Decimal(text)
    with pytest.raises(FrozenInstanceError):
        money.units = 0


@pytest.mark.parametrize("text", ["-1", "0", "0.00", "NaN", "Infinity", "1e3", " 1", "1 ", "+1", ".1", "1.", "1.001", "١٢", "1,00", "92233720368547759", 1.1, True, None])
def test_rejects_invalid_egp_inputs(text):
    with pytest.raises(DomainError):
        Money.from_decimal("EGP", text)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("1E+1000000"), Decimal("-1")])
def test_decimal_rejects_nonfinite_negative_and_extreme(value):
    with pytest.raises(DomainError):
        Money.from_decimal("USDT", value)


def test_money_range_currency_scale_and_json_float_rejection():
    for units in (MAX_UNITS + 1, -MAX_UNITS - 1, True, 1.2):
        with pytest.raises(DomainError):
            checked_units(units)
    with pytest.raises(DomainError):
        Money.from_decimal("USD", "1")
    with pytest.raises(DomainError):
        to_units("1", 19)
    assert not FundingSerializer(data={"amount": 1.2}).is_valid()
    assert decimal_string(-1, 8) == "-0.00000001"


def test_cash_rounds_buys_up_sells_down():
    assert Money.cost("EGP", 1001, 1, 3, 0, round_up=True).units == 101
    assert Money.cost("EGP", 1001, 1, 3, 0, round_up=False).units == 100


@given(budget=st.integers(1, 10**10), price=st.integers(1, 10**8), step=st.integers(1, 100))
def test_affordable_quantity_is_largest_step_within_budget(budget, price, step):
    quantity = Money.affordable_quantity("EGP", budget, price, 4, 3, step)
    assert quantity % step == 0
    if quantity:
        assert Money.cost("EGP", price, quantity, 4, 3, round_up=True).units <= budget
    assert Money.cost("EGP", price, quantity + step, 4, 3, round_up=True).units > budget
