import hashlib
import json

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.translation import gettext as _

from .api import json_value
from .exceptions import DomainError
from .models import IdempotencyRecord


class Idempotency:
    @classmethod
    def execute(cls, actor, operation, key, payload, action):
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise DomainError("idempotency_key_required", _("Provide a valid Idempotency-Key header."))
        fingerprint = hashlib.sha256(json.dumps(
            json_value(payload), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        with transaction.atomic():
            current = get_user_model().objects.only("is_active", "authorization_revision").get(pk=actor.pk)
            if not current.is_active or current.authorization_revision != actor.authorization_revision:
                raise DomainError("authorization_changed", _("Your access changed. Please retry the request."), 403)
            existing = IdempotencyRecord.objects.filter(
                actor=actor, operation=operation, key=key,
            ).first()
            if existing:
                if existing.fingerprint != fingerprint:
                    raise DomainError("idempotency_conflict", _("This key was used for another request."), 409)
                return existing.result
            result = json_value(action())
            IdempotencyRecord.objects.create(
                actor=actor, operation=operation, key=key,
                fingerprint=fingerprint, result=result,
            )
            return result
