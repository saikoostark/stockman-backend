import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.exceptions import DomainError

from .models import WorkerLease


class WorkerCoordinator:
    def __init__(self):
        self.token: str = str(uuid.uuid4())
        self.generation: int | None = None

    def acquire(self):
        with transaction.atomic():
            now = timezone.now()
            lease = WorkerLease.objects.filter(name="market-worker").first()
            if lease and lease.expires_at > now:
                raise DomainError("worker_active", "Another worker holds the lease.", 409)
            generation = lease.generation + 1 if lease else 1
            WorkerLease.objects.update_or_create(name="market-worker", defaults={
                "token": self.token, "generation": generation, "heartbeat_at": now,
                "expires_at": now + timedelta(seconds=settings.WORKER_LEASE_SECONDS),
            })
            self.generation = generation

    def assert_and_renew(self):
        assert self.generation is not None
        now = timezone.now()
        updated = WorkerLease.objects.filter(
            name="market-worker", token=self.token, generation=self.generation,
            expires_at__gt=now,
        ).update(heartbeat_at=now, expires_at=now + timedelta(seconds=settings.WORKER_LEASE_SECONDS))
        if not updated:
            raise DomainError("worker_lease_lost", "The worker lease expired or changed.", 409)

    def release(self):
        WorkerLease.objects.filter(name="market-worker", token=self.token).update(expires_at=timezone.now())

    def effects(self):
        from apps.alerts.services import EvaluateAlerts
        from apps.trading.services import OrderMatcher

        with transaction.atomic():
            self.assert_and_renew()
            OrderMatcher().run()
            EvaluateAlerts.run()


class RetentionService:
    @staticmethod
    def run():
        from apps.events.models import UserEvent
        from apps.markets.models import PriceObservation

        now = timezone.now()
        PriceObservation.objects.filter(source_at__lt=now - timedelta(days=settings.MARKET_HISTORY_DAYS)).delete()
        UserEvent.objects.filter(created_at__lt=now - timedelta(days=settings.EVENT_RETENTION_DAYS)).delete()
