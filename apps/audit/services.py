from apps.common.api import json_value

from .models import AuditEvent


class AuditRecorder:
    SENSITIVE = {"password", "password_confirmation", "token", "secret", "cookie", "authorization", "api_key", "api_token", "csrfmiddlewaretoken", "sessionid"}

    @classmethod
    def redact(cls, value):
        if isinstance(value, dict):
            return {key: "[redacted]" if any(part in key.lower() for part in cls.SENSITIVE)
                    else cls.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls.redact(item) for item in value]
        return value

    @classmethod
    def record(cls, actor, action, target_type, target_id, changes=None, request_id="", reason=""):
        return AuditEvent.objects.create(
            actor=actor if actor and actor.is_authenticated else None,
            action=action, target_type=target_type, target_id=str(target_id),
            changes=cls.redact(json_value(changes or {})),
            request_id=request_id, reason=reason,
        )
