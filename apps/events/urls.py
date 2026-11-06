from django.urls import path

from .stream import EventStreamView
from .views import NotificationListView, NotificationReadView

urlpatterns = [
    path("notifications/", NotificationListView.as_view(), name="notification-list"),
    path("notifications/<uuid:notification_id>/read/", NotificationReadView.as_view(), name="notification-read"),
    path("events/", EventStreamView.as_view(), name="events-stream"),
]
