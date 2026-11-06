from django.conf import settings
from django.utils import timezone

from .binance import BinanceProvider
from .eodhd import EODHDProvider


def configured_providers(*, reserve, clock=timezone.now):
    return (
        BinanceProvider(symbols=getattr(settings, "BINANCE_SYMBOLS", []), reserve=reserve,
                        cadence_seconds=getattr(settings, "MARKET_POLL_SECONDS", 10), clock=clock),
        EODHDProvider(verified=getattr(settings, "EGX_ACCESS_VERIFIED", False),
                      token=getattr(settings, "EODHD_API_TOKEN", ""),
                      symbols=getattr(settings, "EGX_SYMBOLS", []), reserve=reserve, clock=clock),
    )


def configured_capabilities():
    providers = configured_providers(reserve=lambda *_: None)
    try:
        return tuple(provider.capabilities() for provider in providers)
    finally:
        for provider in providers:
            provider.close()
