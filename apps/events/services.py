from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.api import json_value
from apps.common.exceptions import DomainError

from .models import Notification, UserEvent


class EventPublisher:
    @staticmethod
    def publish(user, kind, payload):
        return UserEvent.objects.create(user=user, kind=kind, payload=json_value(payload))


class NotificationPublisher:
    @staticmethod
    def publish(user, translation_key, parameters, deduplication_key):
        with transaction.atomic():
            notification, created = Notification.objects.get_or_create(
                deduplication_key=deduplication_key,
                defaults={"user": user, "translation_key": translation_key,
                          "parameters": json_value(parameters)},
            )
            if created:
                EventPublisher.publish(user, "notification.created", {
                    "id": str(notification.id), "translation_key": translation_key,
                    "parameters": notification.parameters,
                })
            return notification


class ReadNotification:
    @staticmethod
    def execute(user, notification_id):
        from apps.identity.policies import AuthorizationPolicy

        with transaction.atomic():
            AuthorizationPolicy.require(user, "events.read", owner_id=user.pk)
            notification = Notification.objects.filter(user=user, pk=notification_id).first()
            if notification is None:
                raise DomainError("not_found", _("Notification not found."), 404)
            if notification.read_at is None:
                notification.read_at = timezone.now()
                notification.save(update_fields=["read_at"])
                AuditRecorder.record(user, "notification.read", "notification", notification.id)
            return {"id": str(notification.id), "read_at": notification.read_at}
