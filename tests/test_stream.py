import asyncio
from datetime import timedelta

import pytest
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.events.models import UserEvent
from apps.events.services import EventPublisher
from apps.events.stream import EventStreamView, StreamReader


@pytest.fixture
def event_access(monkeypatch):
    monkeypatch.setattr("apps.events.stream.AuthorizationPolicy.allows", lambda *args, **kwargs: True)


def session_for(user):
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.save()
    return session.session_key


@pytest.mark.django_db
def test_stream_uses_current_session_and_revision(user, event_access):
    key = session_for(user)
    batch = StreamReader.read(key, None)
    assert batch["revision"] == 0
    get_user_model().objects.filter(pk=user.pk).update(authorization_revision=1)
    assert StreamReader.read(key, 0, 0) is None
    assert StreamReader.read(key, 0, 1) is not None
    Session.objects.filter(session_key=key).update(expire_date=timezone.now() - timedelta(seconds=1))
    assert StreamReader.read(key, 0) is None


@pytest.mark.django_db
def test_stream_never_returns_other_users_events(user, django_user_model, event_access):
    other = django_user_model.objects.create_user(username="other")
    key = session_for(user)
    own = EventPublisher.publish(user, "notification.created", {"safe": True})
    EventPublisher.publish(other, "notification.created", {"secret": True})
    batch = StreamReader.read(key, own.pk - 1)
    assert [item["payload"] for item in batch["events"]] == [{"safe": True}]
    Session.objects.filter(session_key=key).delete()
    assert StreamReader.read(key, 0) is None


@pytest.mark.django_db
def test_stream_resync_expired_cursor(user, event_access):
    key = session_for(user)
    EventPublisher.publish(user, "notification.created", {})
    EventPublisher.publish(user, "notification.created", {})
    last = EventPublisher.publish(user, "notification.created", {})
    UserEvent.objects.exclude(pk=last.pk).delete()
    result = StreamReader.read(key, 0)
    assert result["reset"]
    assert result["cursor"] == last.pk


@pytest.mark.django_db
def test_password_change_revokes_stream_session(user, event_access):
    key = session_for(user)
    user.set_password("Replacement-pass-934!")
    user.save()
    assert StreamReader.read(key, 0) is None


@pytest.mark.django_db
def test_stream_invalid_cursor_or_anonymous(api_client):
    assert api_client.get("/api/v1/events/", HTTP_LAST_EVENT_ID="invalid").status_code == 400
    assert api_client.get("/api/v1/events/").status_code == 403


def test_frame_keeps_decimal_strings_and_unicode():
    frame = EventStreamView.frame("notification.created", {"amount": "1.00000001", "symbol": "سهم"}, 4)
    assert frame.startswith("id: 4\nevent: notification.created\n")
    assert '"amount":"1.00000001"' in frame


def test_stream_disconnect_cleans_generator(monkeypatch):
    batch = {"revision": 0, "cursor": 0, "events": [], "quotes": [], "reset": False}
    async def run():
        stream = EventStreamView().iterate("session", batch)
        assert await anext(stream) == "retry: 3000\n\n"
        await stream.aclose()
    asyncio.run(run())
