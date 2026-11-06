from drf_spectacular.utils import extend_schema

from apps.common.api import ENVELOPE, APIView, page

from .selectors import NotificationSelector
from .services import ReadNotification


class NotificationListView(APIView):
    @extend_schema(responses=ENVELOPE, tags=["Notifications"])
    def get(self, request):
        items, metadata = page(NotificationSelector.for_user(request.user), request, NotificationSelector.represent)
        return self.respond(items, metadata=metadata)


class NotificationReadView(APIView):
    @extend_schema(request=None, responses=ENVELOPE, tags=["Notifications"])
    def post(self, request, notification_id):
        return self.respond(ReadNotification.execute(request.user, notification_id))
