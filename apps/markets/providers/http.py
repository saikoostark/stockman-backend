import json
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx

from .validation import ProviderError


def retry_seconds(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            at = parsedate_to_datetime(value)
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            seconds = int((at - now).total_seconds()) + 1
        except (ValueError, TypeError, OverflowError):
            return None
    return min(86400, max(1, seconds))


class JsonHTTP:
    def __init__(self, *, base_url, provider, reserve, clock, client=None, transport=None):
        self.base_url = base_url
        self.provider = provider
        self.reserve = reserve
        self.clock = clock
        self.owns_client = client is None
        self.client = client or httpx.Client(
            transport=transport, timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False, trust_env=False,
        )

    def get(self, path: str, *, params=None, weight: int = 1):
        self.reserve(self.provider, weight)
        try:
            with self.client.stream(
                "GET", self.base_url + path, params=params, timeout=10, follow_redirects=False,
            ) as response:
                retry = retry_seconds(response.headers.get("Retry-After"), self.clock())
                if response.status_code in (418, 429):
                    raise ProviderError("RATE_LIMITED", retry_after=retry)
                if response.status_code in (401, 403, 451):
                    raise ProviderError("ACCESS_DENIED", retry_after=retry)
                if response.status_code != 200:
                    raise ProviderError("UPSTREAM_ERROR", retry_after=retry)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise ProviderError("PAYLOAD_TOO_LARGE")
                return json.loads(content, parse_float=Decimal, parse_constant=self._invalid_constant)
        except (httpx.HTTPError, OSError):
            raise ProviderError("NETWORK_ERROR") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("INVALID_PAYLOAD") from None

    @staticmethod
    def _invalid_constant(value):
        raise ValueError("Non-finite JSON number")

    def close(self):
        if self.owns_client:
            self.client.close()


class TokenTransport(httpx.BaseTransport):
    """Inject EODHD's query credential below HTTPX's URL logging boundary.

    The outer client, returned response, and exceptions only see a credential-free URL.
    Never enable wire-level logging for an authenticated transport.
    """

    def __init__(self, token: str, transport=None):
        self._token = token
        self._transport = transport or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        secret_request = httpx.Request(
            request.method, request.url.copy_add_param("api_token", self._token),
            headers=request.headers, stream=request.stream, extensions=request.extensions,
        )
        try:
            return self._transport.handle_request(secret_request)
        except (httpx.HTTPError, OSError):
            raise ProviderError("NETWORK_ERROR") from None
        finally:
            secret_request.url = request.url

    def close(self):
        self._transport.close()
