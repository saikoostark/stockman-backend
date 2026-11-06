from drf_spectacular.utils import extend_schema

from apps.common.api import ENVELOPE, APIView, page

from .selectors import AuditSelector


class AuditView(APIView):
    @extend_schema(responses=ENVELOPE, tags=["Audit"])
    def get(self, request):
        items, metadata = page(AuditSelector.for_user(request.user, request.query_params), request, AuditSelector.represent)
        return self.respond(items, metadata=metadata)
