import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from apps.markets.dtos import InstrumentData
from apps.markets.providers.binance import BinanceProvider
from apps.markets.providers.eodhd import CAIRO, EODHDProvider
from apps.markets.providers.http import retry_seconds
from apps.markets.providers.validation import ProviderError, units

NOW = datetime(2026, 5, 20, 12, tzinfo=UTC)
CRYPTO = InstrumentData("BTCUSDT", "BTC / USDT", "CRYPTO", "USDT", "BINANCE", "BTCUSDT", 2, 3, 1, 1, 500000000)
EGX = InstrumentData("COMI", "COMI", "EGX", "EGP", "EODHD", "COMI.EGX", 2, 0, 1, 1, 0, data_mode="EOD", cadence_seconds=86400, venue_timezone="Africa/Cairo")


def metadata():
    return {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True,
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                                     {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
                                     {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"}]}]}


def trade(**overrides):
    return [{"a": 12, "p": "120.25", "q": "1.000", "T": int(NOW.timestamp() * 1000), **overrides}]


def binance(payload, status=200, headers=None, reserve=None):
    return BinanceProvider(symbols=["BTCUSDT"], reserve=reserve or (lambda *_: None), clock=lambda: NOW,
                           transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload, headers=headers)))


def eodhd(payload, **kwargs):
    return EODHDProvider(verified=kwargs.pop("verified", True), token=kwargs.pop("token", "test-only-secret"),
                         symbols=kwargs.pop("symbols", ["COMI"]), reserve=kwargs.pop("reserve", lambda *_: None),
                         clock=lambda: NOW, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), **kwargs)


def test_metadata_normalizes_only_allowed_spot_usdt():
    provider = binance(metadata())
    data = provider.instruments()[0]
    assert data.price_scale == 2
    assert data.quantity_scale == 3
    assert data.price_step == data.quantity_step == data.minimum_quantity == 1
    assert data.minimum_notional == 500000000
    with pytest.raises(FrozenInstanceError):
        data.name = "changed"
    provider.close()


@pytest.mark.parametrize("field,value", [("status", "BREAK"), ("isSpotTradingAllowed", False), ("symbol", "ETHUSDT")])
def test_metadata_does_not_enable_unsupported_symbols(field, value):
    payload = metadata()
    payload["symbols"][0][field] = value
    assert binance(payload).instruments() == ()


@pytest.mark.parametrize("payload", [None, {}, [], {"symbols": None}, {"symbols": [None]}, {"symbols": [{"symbol": "BTCUSDT"}]}])
def test_malformed_metadata_fails_closed(payload):
    with pytest.raises(ProviderError):
        binance(payload).instruments()


def test_metadata_requires_notional_and_currency_rules():
    payload = metadata()
    payload["symbols"][0]["filters"].pop()
    with pytest.raises(ProviderError, match="MISSING_NOTIONAL_RULE"):
        binance(payload).instruments()
    payload = metadata()
    payload["symbols"][0]["quoteAsset"] = "USD"
    with pytest.raises(ProviderError, match="UNSUPPORTED_INSTRUMENT"):
        binance(payload).instruments()


def test_injected_client_uses_fixed_public_origin_and_timestamped_trade():
    requests = []
    weights = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: requests.append(req) or httpx.Response(200, json=trade())))
    provider = BinanceProvider(symbols=["BTCUSDT"], reserve=lambda *v: weights.append(v), client=client, clock=lambda: NOW)
    quote = provider.latest(CRYPTO)
    assert quote.price_units == 12025
    assert quote.source_at == NOW
    assert quote.observation_id == "binance:agg:12"
    assert requests[0].url.host == "data-api.binance.vision"
    assert requests[0].url.path == "/api/v3/aggTrades"
    assert "api_key" not in str(requests[0].url)
    assert weights == [("BINANCE", 4)]
    provider.close()
    assert not client.is_closed
    client.close()


@pytest.mark.parametrize("override", [{"p": "NaN"}, {"p": "1e3"}, {"p": "1.001"}, {"p": "0"}, {"p": "9999999999999999999999"}, {"q": "0.0001"}, {"T": True}, {"T": int((NOW + timedelta(days=1)).timestamp() * 1000)}, {"a": -1}])
def test_trade_rejects_invalid_precision_overflow_and_timestamps(override):
    with pytest.raises(ProviderError):
        binance(trade(**override)).latest(CRYPTO)


@pytest.mark.parametrize("payload", [[], {}, [{"p": "12.00"}], None, [None]])
def test_undated_or_malformed_prices_never_get_fetch_timestamp(payload):
    with pytest.raises(ProviderError):
        binance(payload).latest(CRYPTO)


@pytest.mark.parametrize("status,code", [(429, "RATE_LIMITED"), (418, "RATE_LIMITED"), (451, "ACCESS_DENIED"), (500, "UPSTREAM_ERROR"), (302, "UPSTREAM_ERROR")])
def test_errors_are_sanitized_and_retry_after_preserved(status, code):
    with pytest.raises(ProviderError) as exc:
        binance({"error": "secret vendor detail"}, status, {"Retry-After": "123"}).latest(CRYPTO)
    assert exc.value.code == code
    assert exc.value.retry_after == 123
    assert "secret" not in str(exc.value)


def test_transport_error_is_sanitized():
    def failed(req):
        raise httpx.ReadTimeout("sensitive URL")
    provider = BinanceProvider(symbols=["BTCUSDT"], reserve=lambda *_: None, clock=lambda: NOW, transport=httpx.MockTransport(failed))
    with pytest.raises(ProviderError, match="NETWORK_ERROR") as exc:
        provider.latest(CRYPTO)
    assert "sensitive" not in str(exc.value)


def test_historical_klines_exclude_unfinished_bar():
    start = int((NOW - timedelta(minutes=1)).timestamp() * 1000)
    payload = [[start, "100.00", "121.00", "99.00", "120.00", "2.000", start + 59999],
               [start + 60000, "120.00", "121.00", "99.00", "120.00", "2.000", start + 119999]]
    bars = binance(payload).history(CRYPTO)
    assert len(bars) == 1
    assert bars[0].close_units == 12000
    assert bars[0].closed_at < NOW


@pytest.mark.parametrize("kwargs,reason", [({"verified": False}, "EGX_ACCESS_NOT_VERIFIED"), ({"token": ""}, "EGX_TOKEN_NOT_CONFIGURED"), ({"symbols": []}, "EGX_SYMBOLS_NOT_CONFIGURED")])
def test_egx_unavailable_gate_never_calls_transport(kwargs, reason):
    calls = []
    provider = eodhd([], reserve=lambda *_: calls.append(1), **kwargs)
    caps = provider.capabilities()
    assert not caps.available
    assert not caps.entitlement_verified
    assert caps.reason == reason
    with pytest.raises(ProviderError, match=reason):
        provider.latest(EGX)
    assert calls == []


def eodbar(day="2026-05-19", **kwargs):
    return {"date": day, "open": "100.00", "high": "110.00", "low": "90.00", "close": "105.00", "adjusted_close": "105.00", "volume": 1000, **kwargs}


def test_eod_date_only_is_completed_previous_cairo_day():
    provider = eodhd([eodbar(), eodbar("2026-05-20")])
    quote = provider.latest(EGX)
    local = quote.source_at.astimezone(CAIRO)
    assert local.date().isoformat() == "2026-05-19"
    assert (local.hour, local.minute, local.second) == (23, 59, 59)
    assert quote.session_date == "2026-05-19"
    assert quote.source_at != NOW
    assert provider.instruments()[0].provider_symbol == "COMI.EGX"


@pytest.mark.parametrize("row", [eodbar(adjusted_close="52.50"), eodbar(split=2), eodbar(dividend=1)])
def test_corporate_action_adjustments_halt(row):
    with pytest.raises(ProviderError, match="CORPORATE_ACTION_UNSUPPORTED"):
        eodhd([row]).latest(EGX)


def test_eod_token_is_absent_from_httpx_logs_and_response_url(caplog):
    calls = []
    def transport(req):
        calls.append(req.url.params["api_token"])
        return httpx.Response(200, json=[eodbar()])
    provider = EODHDProvider(verified=True, token="test-only-secret", symbols=["COMI"], reserve=lambda *_: None,
                             clock=lambda: NOW, transport=httpx.MockTransport(transport))
    with caplog.at_level(logging.DEBUG):
        provider.latest(EGX)
    assert calls == ["test-only-secret"]
    assert "test-only-secret" not in caplog.text
    provider.close()


@pytest.mark.parametrize("value,scale", [(1.2, 2), ("0.001", 2), ("-1", 2), ("9223372036854775808", 0), (True, 0), ("∞", 2)])
def test_checked_integer_conversion_rejects_invalid_values(value, scale):
    with pytest.raises(ProviderError):
        units(value, scale)


def test_integer_conversion_does_not_round_with_decimal_context():
    assert units(Decimal("92233720368547758.07"), 2) == 2**63 - 1
    assert retry_seconds("Wed, 20 May 2026 12:01:00 GMT", NOW) == 61
