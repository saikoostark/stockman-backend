from datetime import timedelta

import pytest
from django.utils import timezone

from apps.common.exceptions import DomainError
from apps.operations.models import WorkerLease
from apps.operations.services import WorkerCoordinator


@pytest.mark.django_db
def test_worker_lease_excludes_competitor_and_fences_old_owner():
    first = WorkerCoordinator()
    second = WorkerCoordinator()
    first.acquire()
    with pytest.raises(DomainError, match="Another worker"):
        second.acquire()
    WorkerLease.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    second.acquire()
    with pytest.raises(DomainError, match="expired or changed"):
        first.assert_and_renew()
    second.assert_and_renew()
    first.release()
    assert WorkerLease.objects.get().token == second.token
    second.release()
    assert WorkerLease.objects.get().expires_at <= timezone.now()


@pytest.mark.django_db
def test_health_reports_worker_state(api_client):
    response = api_client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.data["data"]["worker"] == "stopped"
    worker = WorkerCoordinator()
    worker.acquire()
    assert api_client.get("/api/v1/health/").data["data"]["worker"] == "running"
