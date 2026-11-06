"""Gated EODHD EGX adapter. Configured verification is not a claim of free access."""

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apps.markets.dtos import CandleData, InstrumentData, ProviderCapabilities, QuoteData

from .http import JsonHTTP, TokenTransport
from .validation import ProviderError, decimal_value, integer, units

CAIRO = ZoneInfo("Africa/Cairo")


class EODHDProvider:
    def __init__(self, *, verified, token, symbols, reserve, transport=None, clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.symbols = tuple(dict.fromkeys(symbols))
        self.reason = ""
        if not verified:
            self.reason = "EGX_ACCESS_NOT_VERIFIED"
        elif not token:
            self.reason = "EGX_TOKEN_NOT_CONFIGURED"
        elif not self.symbols:
            self.reason = "EGX_SYMBOLS_NOT_CONFIGURED"
        elif len(self.symbols) > 20 or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9_-]{1,30}(?:\.EGX)?", s) for s in self.symbols):
            self.reason = "INVALID_EGX_SYMBOLS"
        self.symbols = tuple(dict.fromkeys(s if s.endswith(".EGX") else f"{s}.EGX" for s in self.symbols if isinstance(s, str)))
        self.http = JsonHTTP(base_url="https://eodhd.com/api", provider="EODHD", reserve=reserve,
                             clock=self.clock, transport=TokenTransport(token, transport))

    def capabilities(self):
        return ProviderCapabilities("EODHD", "EGX", "EGP", "Africa/Cairo", "EOD", 86400, 0, 1000,
                                    not bool(self.reason), not bool(self.reason), self.reason)

    def _ready(self):
        if self.reason:
            raise ProviderError(self.reason)

    def instruments(self) -> tuple[InstrumentData, ...]:
        self._ready()
        # No guessed exchange catalog or synthetic quotes. Only operator-verified mappings.
        return tuple(InstrumentData(symbol=s.removesuffix(".EGX"), name=s.removesuffix(".EGX"),
                                    market="EGX", currency="EGP", provider="EODHD", provider_symbol=s,
                                    price_scale=2, quantity_scale=0, quantity_step=1,
                                    minimum_quantity=1, minimum_notional=0, data_mode="EOD",
                                    cadence_seconds=86400, venue_timezone="Africa/Cairo")
                     for s in self.symbols)

    def history(self, instrument: InstrumentData, *, limit: int = 100) -> tuple[CandleData, ...]:
        self._ready()
        if instrument.provider != "EODHD" or instrument.provider_symbol not in self.symbols or instrument.currency != "EGP" or instrument.data_mode != "EOD":
            raise ProviderError("UNSUPPORTED_INSTRUMENT")
        limit = integer(limit, minimum=1, maximum=1000)
        today = self.clock().astimezone(CAIRO).date()
        payload = self.http.get(f"/eod/{instrument.provider_symbol}", params={
            "fmt": "json", "period": "d", "order": "a",
            "from": (today - timedelta(days=max(14, limit * 3))).isoformat(),
            "to": (today - timedelta(days=1)).isoformat(),
        })
        try:
            if not isinstance(payload, list) or len(payload) > 3100:
                raise ProviderError("INVALID_PAYLOAD")
            bars = []
            previous_date = None
            for row in payload:
                if not isinstance(row["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"], re.ASCII):
                    raise ProviderError("INVALID_SESSION_DATE")
                session = date.fromisoformat(row["date"])
                if session > today:
                    raise ProviderError("FUTURE_TIMESTAMP")
                if session == today:
                    continue
                if session < date(2000, 1, 1) or (previous_date and session <= previous_date):
                    raise ProviderError("OUT_OF_ORDER_HISTORY")
                previous_date = session
                # Mixing adjusted and unadjusted scales requires corporate-action support.
                if "adjusted_close" not in row or decimal_value(row["adjusted_close"]) != decimal_value(row["close"]):
                    raise ProviderError("CORPORATE_ACTION_UNSUPPORTED")
                if row.get("split", 1) != 1 or row.get("dividend", 0) != 0:
                    raise ProviderError("CORPORATE_ACTION_UNSUPPORTED")
                op, high, low, close = [units(row[key], instrument.price_scale) for key in ("open", "high", "low", "close")]
                if low > min(op, close) or high < max(op, close):
                    raise ProviderError("INVALID_BAR_PRICES")
                opened = datetime.combine(session, time.min, CAIRO).astimezone(UTC)
                # Date-only data has no intraday publication time. Require the entire local day to end.
                closed = (datetime.combine(session + timedelta(days=1), time.min, CAIRO) - timedelta(microseconds=1)).astimezone(UTC)
                bars.append(CandleData(instrument.provider_symbol, f"eodhd:eod:{session.isoformat()}", "1d", opened, closed,
                                       op, high, low, close, units(row["volume"], instrument.quantity_scale, positive=False)))
            return tuple(bars[-limit:])
        except (KeyError, TypeError, IndexError, ValueError, OverflowError):
            raise ProviderError("INVALID_PAYLOAD") from None

    def latest(self, instrument: InstrumentData) -> QuoteData:
        bars = self.history(instrument, limit=2)
        if not bars:
            raise ProviderError("NO_COMPLETED_SESSION")
        bar = bars[-1]
        return QuoteData(instrument.provider_symbol, bar.close_units, bar.closed_at, bar.observation_id,
                         bar.closed_at.astimezone(CAIRO).date().isoformat())

    def close(self):
        self.http.close()
