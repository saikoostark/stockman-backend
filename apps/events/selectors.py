from apps.identity.policies import AuthorizationPolicy

from .models import Notification


class NotificationSelector:
    @staticmethod
    def for_user(user):
        AuthorizationPolicy.require(user, "events.read", owner_id=user.pk)
        return Notification.objects.filter(user=user)

    @staticmethod
    def represent(notification):
        return {
            "id": notification.id, "translation_key": notification.translation_key,
            "parameters": notification.parameters, "created_at": notification.created_at,
            "read_at": notification.read_at,
        }
