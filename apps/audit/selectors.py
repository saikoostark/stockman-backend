from apps.identity.policies import AuthorizationPolicy

from .models import AuditEvent


class AuditSelector:
    @staticmethod
    def for_user(user, parameters):
        AuthorizationPolicy.require(user, "audit.read")
        queryset = AuthorizationPolicy.scope(user, "audit.read", AuditEvent.objects.all(), owner_field="actor_id")
        if parameters.get("action"):
            queryset = queryset.filter(action=parameters["action"])
        if parameters.get("target_type"):
            queryset = queryset.filter(target_type=parameters["target_type"])
        return queryset

    @staticmethod
    def represent(event):
        return {
            "id": event.id, "actor_id": event.actor_id, "action": event.action,
            "target_type": event.target_type, "target_id": event.target_id,
            "changes": event.changes, "request_id": event.request_id,
            "reason": event.reason, "created_at": event.created_at,
        }
