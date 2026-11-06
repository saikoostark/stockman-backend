from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny

from apps.common.api import ENVELOPE, APIView

from .models import WorkerLease


class HealthView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses=ENVELOPE, tags=["Operations"])
    def get(self, request):
        lease = WorkerLease.objects.filter(name="market-worker").first()
        return self.respond({
            "status": "ok", "paper_trading": True,
            "worker": "running" if lease and lease.expires_at > timezone.now() else "stopped",
            "worker_heartbeat": lease.heartbeat_at if lease else None,
        })
