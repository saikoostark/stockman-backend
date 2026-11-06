"""Only Binance's unauthenticated public spot market-data endpoints."""

import json
import math
import re
from datetime import UTC, datetime

from apps.markets.dtos import CandleData, InstrumentData, ProviderCapabilities, QuoteData

from .http import JsonHTTP
from .validation import ProviderError, integer, step, timestamp_ms, units

BINANCE_ORIGIN = "https://data-api.binance.vision"


class BinanceProvider:
    def __init__(self, *, symbols, reserve, cadence_seconds=10, client=None, transport=None, clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.symbols = tuple(dict.fromkeys(symbols))
        if len(self.symbols) > 50 or any(
            not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{2,30}USDT", s)
            for s in self.symbols
        ):
            raise ProviderError("INVALID_ALLOWLIST")
        self.cadence = min(3600, max(5, cadence_seconds))
        self.http = JsonHTTP(base_url=BINANCE_ORIGIN, provider="BINANCE", reserve=reserve,
                             clock=self.clock, client=client, transport=transport)

    def capabilities(self):
        return ProviderCapabilities(
            "BINANCE", "CRYPTO", "USDT", "UTC", "LIVE", self.cadence, 0, 1000,
            False, bool(self.symbols), "" if self.symbols else "NO_SYMBOLS_CONFIGURED",
        )

    def instruments(self) -> tuple[InstrumentData, ...]:
        if not self.symbols:
            return ()
        payload = self.http.get("/api/v3/exchangeInfo", params={"symbols": json.dumps(self.symbols, separators=(",", ":"))}, weight=20)
        try:
            rows = payload["symbols"]
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ProviderError("INVALID_METADATA")
            result = []
            seen = set()
            for item in rows:
                symbol = item["symbol"]
                if symbol not in self.symbols:
                    continue
                if symbol in seen:
                    raise ProviderError("INVALID_METADATA")
                seen.add(symbol)
                if item["status"] != "TRADING" or item["isSpotTradingAllowed"] is not True:
                    continue
                if item["quoteAsset"] != "USDT" or symbol != item["baseAsset"] + "USDT":
                    raise ProviderError("UNSUPPORTED_INSTRUMENT")
                rules = item["filters"]
                if not isinstance(rules, list):
                    raise ProviderError("INVALID_METADATA")
                filters = {f["filterType"]: f for f in rules}
                if len(filters) != len(rules):
                    raise ProviderError("INVALID_METADATA")
                price_scale, price_step = step(filters["PRICE_FILTER"]["tickSize"])
                quantity_scale, quantity_step = step(filters["LOT_SIZE"]["stepSize"])
                minimum_quantity = units(filters["LOT_SIZE"]["minQty"], quantity_scale)
                market_lot = filters.get("MARKET_LOT_SIZE")
                if market_lot and units(market_lot["stepSize"], 18, positive=False):
                    market_scale, market_step = step(market_lot["stepSize"])
                    combined_scale = max(quantity_scale, market_scale)
                    quantity_step = math.lcm(quantity_step * 10 ** (combined_scale - quantity_scale), market_step * 10 ** (combined_scale - market_scale))
                    minimum_quantity *= 10 ** (combined_scale - quantity_scale)
                    quantity_scale = combined_scale
                    minimum_quantity = max(minimum_quantity, units(market_lot["minQty"], quantity_scale, positive=False))
                minimum_quantity = ((minimum_quantity + quantity_step - 1) // quantity_step) * quantity_step
                notional_rules = [filters[key] for key in ("NOTIONAL", "MIN_NOTIONAL") if key in filters]
                if not notional_rules:
                    raise ProviderError("MISSING_NOTIONAL_RULE")
                minimum_notional = max(units(rule["minNotional"], 8, positive=False) for rule in notional_rules)
                integer(quantity_step, minimum=1)
                integer(minimum_quantity, minimum=1)
                result.append(InstrumentData(
                    symbol=symbol, name=f'{item["baseAsset"]} / USDT', market="CRYPTO", currency="USDT",
                    provider="BINANCE", provider_symbol=symbol, price_scale=price_scale,
                    quantity_scale=quantity_scale, quantity_step=quantity_step,
                    minimum_quantity=minimum_quantity, minimum_notional=minimum_notional,
                    price_step=price_step, cadence_seconds=self.cadence,
                ))
            return tuple(result)
        except (KeyError, TypeError, IndexError, AttributeError):
            raise ProviderError("INVALID_METADATA") from None

    def _check(self, instrument):
        if instrument.provider != "BINANCE" or instrument.provider_symbol not in self.symbols or instrument.market != "CRYPTO" or instrument.currency != "USDT":
            raise ProviderError("UNSUPPORTED_INSTRUMENT")

    def latest(self, instrument: InstrumentData) -> QuoteData:
        self._check(instrument)
        payload = self.http.get("/api/v3/aggTrades", params={"symbol": instrument.provider_symbol, "limit": 1}, weight=4)
        try:
            if not isinstance(payload, list) or len(payload) != 1:
                raise ProviderError("NO_OBSERVATION" if payload == [] else "INVALID_PAYLOAD")
            row = payload[0]
            price = units(row["p"], instrument.price_scale)
            units(row["q"], instrument.quantity_scale)
            if price % instrument.price_step:
                raise ProviderError("INVALID_PRICE_STEP")
            source_at = timestamp_ms(row["T"], self.clock())
            trade_id = integer(row["a"])
            return QuoteData(instrument.provider_symbol, price, source_at, f"binance:agg:{trade_id}")
        except (KeyError, TypeError, IndexError):
            raise ProviderError("INVALID_PAYLOAD") from None

    def history(self, instrument: InstrumentData, *, limit: int = 100) -> tuple[CandleData, ...]:
        self._check(instrument)
        limit = integer(limit, minimum=1, maximum=1000)
        payload = self.http.get("/api/v3/klines", params={"symbol": instrument.provider_symbol, "interval": "1m", "limit": limit}, weight=2)
        try:
            if not isinstance(payload, list) or len(payload) > limit:
                raise ProviderError("INVALID_PAYLOAD")
            now = self.clock()
            result: list[CandleData] = []
            for row in payload:
                if not isinstance(row, list) or len(row) < 7:
                    raise ProviderError("INVALID_PAYLOAD")
                opened = timestamp_ms(row[0], now)
                close_ms = integer(row[6], minimum=946684800000, maximum=4102444800000)
                # The last returned kline may still be forming. It is not history.
                if close_ms >= int(now.timestamp() * 1000):
                    continue
                closed = timestamp_ms(close_ms, now)
                if closed <= opened or (closed - opened).total_seconds() > 60:
                    raise ProviderError("INVALID_BAR_TIME")
                prices = [units(row[i], instrument.price_scale) for i in range(1, 5)]
                op, high, low, close = prices
                if any(price % instrument.price_step for price in prices) or low > min(op, close) or high < max(op, close):
                    raise ProviderError("INVALID_BAR_PRICES")
                volume = units(row[5], instrument.quantity_scale, positive=False)
                if result and opened <= result[-1].opened_at:
                    raise ProviderError("OUT_OF_ORDER_HISTORY")
                result.append(CandleData(instrument.provider_symbol, f"binance:1m:{row[0]}", "1m", opened, closed, op, high, low, close, volume))
            return tuple(result)
        except (KeyError, TypeError, IndexError):
            raise ProviderError("INVALID_PAYLOAD") from None

    def close(self):
        self.http.close()
