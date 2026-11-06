import asyncio
import json
import time
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.models import Session
from django.db import close_old_connections
from django.db.models import Max, Min
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views import View

from apps.common.api import envelope, json_value
from apps.identity.policies import AuthorizationPolicy
from apps.markets.models import Quote

from .models import UserEvent


class StreamReader:
    @staticmethod
    def session_user(session_key):
        session = Session.objects.filter(session_key=session_key, expire_date__gt=timezone.now()).first()
        if session is None:
            return None
        decoded = session.get_decoded()
        session_user_id = decoded.get(SESSION_KEY)
        if not session_user_id:
            return None
        user = get_user_model().objects.filter(pk=session_user_id, is_active=True).first()
        if user is None or not constant_time_compare(decoded.get(HASH_SESSION_KEY, ""), user.get_session_auth_hash()):
            return None
        if not AuthorizationPolicy.allows(user, "events.read", owner_id=user.pk):
            return None
        return user

    @classmethod
    def authorized(cls, session_key, revision):
        user = cls.session_user(session_key)
        return user is not None and user.authorization_revision == revision

    @classmethod
    def read(cls, session_key, cursor, expected_revision=None):
        user = cls.session_user(session_key)
        if user is None or (expected_revision is not None and user.authorization_revision != expected_revision):
            return None
        records = UserEvent.objects.filter(user=user)
        bounds = records.aggregate(first=Min("id"), last=Max("id"))
        maximum = bounds["last"] or 0
        first = bounds["first"] or 0
        reset = cursor is not None and (cursor > maximum or (first > 0 and cursor < first - 1))
        if cursor is None or reset:
            cursor = maximum
        events = []
        for event in records.filter(id__gt=cursor)[:100]:
            cursor = event.id
            action = None
            if event.kind.startswith(("wallet", "funding")):
                action = "wallets.read"
            elif event.kind.startswith(("order", "trade", "fill")):
                action = "trading.read"
            if action and not AuthorizationPolicy.allows(
                user, action, owner_id=user.pk,
                market=event.payload.get("market"), currency=event.payload.get("currency"),
            ):
                continue
            events.append({"id": event.id, "kind": event.kind, "payload": event.payload})
        quotes = []
        for quote in Quote.objects.select_related("instrument").filter(instrument__enabled=True)[:100]:
            instrument = quote.instrument
            if not AuthorizationPolicy.allows(user, "markets.read", market=instrument.market, currency=instrument.currency):
                continue
            price = format(Decimal(quote.price_units).scaleb(-instrument.price_scale), "f")
            age = (timezone.now() - quote.source_at).total_seconds()
            quotes.append({"instrument_id": str(instrument.pk), "symbol": instrument.symbol,
                           "price": price, "currency": instrument.currency,
                           "source_at": quote.source_at, "received_at": quote.received_at,
                           "observation_id": quote.observation_id, "provider": instrument.provider,
                           "data_mode": instrument.data_mode, "delay_seconds": instrument.delay_seconds,
                           "stale": age > instrument.delay_seconds + max(settings.QUOTE_MAX_AGE_SECONDS, instrument.cadence_seconds * 2)})
        return {"revision": user.authorization_revision, "cursor": cursor,
                "reset": reset, "events": events, "quotes": json_value(quotes)}


class EventStreamView(View):
    async def get(self, request):
        raw = request.headers.get("Last-Event-ID")
        if raw is not None and (not raw.isascii() or not raw.isdigit() or len(raw) > 19):
            return JsonResponse(envelope(request=request, errors=[{
                "code": "invalid_cursor", "message": "Invalid event cursor.", "fields": None,
            }]), status=400)
        cursor = int(raw) if raw is not None else None
        session_key = request.session.session_key
        if not session_key:
            return self.denied(request)
        batch = await sync_to_async(StreamReader.read)(session_key, cursor)
        await sync_to_async(close_old_connections)()
        if batch is None:
            return self.denied(request)
        response = StreamingHttpResponse(self.iterate(session_key, batch), content_type="text/event-stream")
        response["Cache-Control"] = "no-store"
        response["X-Accel-Buffering"] = "no"
        return response

    @staticmethod
    def denied(request):
        return JsonResponse(envelope(request=request, errors=[{
            "code": "permission_denied", "message": "Authentication and event access are required.", "fields": None,
        }]), status=403)

    @staticmethod
    def frame(kind, payload, event_id=None):
        prefix = f"id: {event_id}\n" if event_id is not None else ""
        return f"{prefix}event: {kind}\ndata: {json.dumps(json_value(payload), separators=(',', ':'))}\n\n"

    async def iterate(self, session_key, batch):
        revision = batch["revision"]
        previous_quotes = None
        heartbeat = time.monotonic()
        try:
            yield "retry: 3000\n\n"
            yield self.frame("ready", {"revision": revision}, batch["cursor"] if not batch["events"] else batch["events"][0]["id"] - 1)
            while batch is not None:
                if batch["reset"]:
                    yield self.frame("resync", {"reason": "cursor_expired", "cursor": batch["cursor"]}, batch["cursor"])
                for event in batch["events"]:
                    if not await sync_to_async(StreamReader.authorized)(session_key, revision):
                        return
                    yield self.frame(event["kind"], event["payload"], event["id"])
                if batch["quotes"] != previous_quotes:
                    if not await sync_to_async(StreamReader.authorized)(session_key, revision):
                        return
                    yield self.frame("market.snapshot", batch["quotes"])
                    previous_quotes = batch["quotes"]
                if time.monotonic() - heartbeat >= settings.SSE_HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(settings.SSE_POLL_SECONDS)
                batch = await sync_to_async(StreamReader.read)(session_key, batch["cursor"], revision)
                await sync_to_async(close_old_connections)()
        except asyncio.CancelledError:
            raise
