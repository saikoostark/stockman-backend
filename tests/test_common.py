from dataclasses import dataclass
from decimal import Decimal

import pytest
from django.db import transaction
from rest_framework.test import APIRequestFactory

from apps.audit.models import AuditEvent
from apps.audit.services import AuditRecorder
from apps.common.api import envelope, exception_handler, json_value, page
from apps.common.exceptions import DomainError
from apps.common.idempotency import Idempotency
from apps.common.models import IdempotencyRecord
from apps.events.models import Notification, UserEvent
from apps.events.services import EventPublisher, NotificationPublisher


@dataclass(frozen=True)
class SampleDTO:
    amount: Decimal


def test_dto_preserves_decimal_string():
    assert json_value(SampleDTO(Decimal("123456789012345.00000001"))) == {"amount": "123456789012345.00000001"}
    assert envelope([SampleDTO(Decimal("1.20"))])["data"] == [{"amount": "1.20"}]


def test_domain_error_envelope():
    response = exception_handler(DomainError("blocked", "No", 409, {"amount": "low"}), {})
    assert response.status_code == 409
    assert response.data["errors"][0]["code"] == "blocked"
    assert response.data["data"] is None


@pytest.mark.django_db
def test_idempotency_replays_one_effect(user):
    calls = []
    def action():
        calls.append(True)
        return {"amount": "12.00"}
    first = Idempotency.execute(user, "fund", "key", {"amount": "12.00"}, action)
    assert Idempotency.execute(user, "fund", "key", {"amount": "12.00"}, action) == first
    assert calls == [True]
    assert IdempotencyRecord.objects.count() == 1
    with pytest.raises(DomainError, match="another request"):
        Idempotency.execute(user, "fund", "key", {"amount": "13.00"}, action)


@pytest.mark.django_db
def test_idempotency_rolls_back_all_effects(user):
    def fail():
        AuditRecorder.record(user, "test", "user", user.pk)
        EventPublisher.publish(user, "test.event", {})
        raise DomainError("failed", "Injected failure")
    with pytest.raises(DomainError):
        Idempotency.execute(user, "test", "key", {}, fail)
    assert not AuditEvent.objects.exists()
    assert not UserEvent.objects.exists()
    assert not IdempotencyRecord.objects.exists()


@pytest.mark.django_db
def test_audit_redacts_nested_secrets_and_rejects_instance_edits(user):
    event = AuditRecorder.record(user, "test", "user", user.pk, {
        "api_token": "never store", "nested": {"password": "secret", "currency": "EGP"},
    })
    assert event.changes["api_token"] == "[redacted]"
    assert event.changes["nested"]["password"] == "[redacted]"
    assert event.changes["nested"]["currency"] == "EGP"
    with pytest.raises(ValueError, match="append-only"):
        event.save()
    with pytest.raises(ValueError, match="append-only"):
        event.delete()


@pytest.mark.django_db
def test_notification_content_is_neutral_and_deduplicated(user):
    for _ in range(2):
        NotificationPublisher.publish(user, "alerts.price_above", {"price": "10.00"}, "crossing-1")
    assert Notification.objects.count() == 1
    assert UserEvent.objects.count() == 1
    assert Notification.objects.get().parameters["price"] == "10.00"


@pytest.mark.django_db
def test_notification_and_event_rollback(user):
    with pytest.raises(RuntimeError), transaction.atomic():
        NotificationPublisher.publish(user, "alerts.price_above", {}, "rollback")
        raise RuntimeError("injected")
    assert Notification.objects.count() == UserEvent.objects.count() == 0


@pytest.mark.parametrize("key", [None, "", " ", "a" * 129, 123])
@pytest.mark.django_db
def test_bad_idempotency_key(user, key):
    with pytest.raises(DomainError):
        Idempotency.execute(user, "fund", key, {}, lambda: {})


@pytest.mark.django_db
def test_pagination_bounded_and_invalid(user):
    from rest_framework.request import Request
    request = Request(APIRequestFactory().get("/?page_size=1000"))
    _, metadata = page(AuditEvent.objects.all(), request, lambda event: event.id)
    assert metadata["pagination"]["page_size"] == 100
    bad = Request(APIRequestFactory().get("/?page=not-an-int"))
    with pytest.raises(DomainError):
        page(AuditEvent.objects.all(), bad, lambda event: event.id)
